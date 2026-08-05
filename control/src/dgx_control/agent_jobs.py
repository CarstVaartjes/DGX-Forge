"""Transactional, node-scoped agent operation queue with lease fencing."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from dgx_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProgress,
    AgentResult,
    canonical_message,
)
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .auth import AgentSource
from .logging import redact_text
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    Job,
    Observation,
    Reconciliation,
    ReconciliationOperation,
)
from .models import AgentOperation as StoredOperation

AgentFence = str | AgentClaim | AgentProgress | AgentResult
ResultConsumer = Callable[
    [Session, StoredOperation, AgentOperationAttempt, AgentResult], None
]
ContactConsumer = Callable[[Session, AgentSource], None]
_SAFE_AUTOMATIC_RECLAIM = frozenset({
    AgentOperation.NODE_PROBE.value,
    AgentOperation.WORKLOAD_HEALTH.value,
    AgentOperation.WORKLOAD_VERIFY.value,
})
_TERMINAL_PARENT_STATES = frozenset({"succeeded", "failed", "waiting-for-operator", "expired"})
_RETRY_DISPOSITION = "retry"
_IMPLEMENTED_CAPABILITIES = frozenset(
    {
        AgentOperation.NODE_PROBE.value,
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.WORKLOAD_VERIFY.value,
    }
)


class StaleAgentAttempt(RuntimeError):
    """An agent attempted to update an operation it no longer owns."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _document(value: Mapping[str, object]) -> dict[str, object]:
    """Return the protocol's validated, deterministic JSON representation."""
    return json.loads(canonical_message(value))


class AgentJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        result_consumer: ResultConsumer | None = None,
        contact_consumer: ContactConsumer | None = None,
    ) -> None:
        if result_consumer is not None and not callable(result_consumer):
            raise TypeError("agent result consumer must be callable")
        if contact_consumer is not None and not callable(contact_consumer):
            raise TypeError("agent contact consumer must be callable")
        self._sessions = sessions
        self._clock = clock
        self._result_consumer = result_consumer
        self._contact_consumer = contact_consumer
        self._configuration_lock = threading.Lock()
        self._started = False
        # SQLite ignores row locks. This only prevents same-service test races;
        # PostgreSQL correctness is provided by the database locks below.
        self._claim_lock = threading.RLock()
        self._available = threading.Condition()

    def enqueue(
        self,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
        payload: Mapping[str, object],
    ) -> StoredOperation:
        with self._sessions.begin() as session:
            stored = self.enqueue_in_session(
                session,
                parent_job_id,
                node_id,
                operation,
                base_commit,
                payload,
                operation_id=str(uuid.uuid4()),
            )
        self.notify_available()
        return stored

    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> StoredOperation:
        """Attach a caller-identified operation to the caller's transaction."""
        self._mark_started()
        now = self._clock()
        protocol_operation = AgentOperation(operation)
        validated = AgentClaim(
            schema_version=1,
            job_id=parent_job_id,
            operation_id=operation_id,
            attempt=1,
            fence=str(uuid.uuid4()),
            node_id=node_id,
            operation=protocol_operation,
            base_commit=base_commit,
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=payload,
            deadline=now,
        )
        stored = StoredOperation(
            id=validated.operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=protocol_operation.value,
            payload_digest=validated.payload_digest,
            payload=_document(validated.payload),
            base_commit=base_commit,
            state="queued",
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
        if node is None:
            raise KeyError(node_id)
        if node.state != "active" or node.revoked_at is not None:
            raise ValueError("agent operation node must be active")
        parent = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if parent is None:
            raise KeyError(parent_job_id)
        if parent.state in _TERMINAL_PARENT_STATES:
            raise ValueError("cannot enqueue an agent operation beneath a terminal parent")
        if parent.base_commit != base_commit:
            raise ValueError("agent operation base commit must match its parent")
        if node_id not in parent.targets:
            raise ValueError("agent operation node must be a parent target")
        session.add(stored)
        session.flush()
        return stored

    def notify_available(self) -> None:
        """Wake long polls after a caller-managed enqueue transaction commits."""
        with self._available:
            self._available.notify_all()

    def set_result_consumer(self, consumer: ResultConsumer) -> None:
        """Bind projection consumption once, before the queue serves any work."""
        if not callable(consumer):
            raise TypeError("agent result consumer must be callable")
        with self._configuration_lock:
            if self._result_consumer is not None:
                raise RuntimeError("agent result consumer is already configured")
            if self._started:
                raise RuntimeError("agent job service has already started")
            self._result_consumer = consumer

    def set_contact_consumer(self, consumer: ContactConsumer) -> None:
        """Bind atomic authenticated contact persistence before serving work."""

        if not callable(consumer):
            raise TypeError("agent contact consumer must be callable")
        with self._configuration_lock:
            if self._contact_consumer is not None:
                raise RuntimeError("agent contact consumer is already configured")
            if self._started:
                raise RuntimeError("agent job service has already started")
            self._contact_consumer = consumer

    def _mark_started(self) -> None:
        with self._configuration_lock:
            self._started = True

    def claim(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        wait_seconds: float = 0,
        protocol_version: int | None = None,
        capabilities: Sequence[str] | None = None,
        *,
        source: AgentSource | None = None,
    ) -> AgentClaim | None:
        self._mark_started()
        if (
            not node_id.strip()
            or not certificate_serial.strip()
            or lease_seconds <= 0
            or isinstance(wait_seconds, bool)
            or not 0 <= wait_seconds <= 60
            or (
                protocol_version is not None
                and (
                    isinstance(protocol_version, bool)
                    or not isinstance(protocol_version, int)
                    or not 1 <= protocol_version <= 2_147_483_647
                )
            )
        ):
            raise ValueError("node, certificate, and positive lease are required")
        advertised = self._capabilities(capabilities)
        deadline = time.monotonic() + wait_seconds
        with self._available:
            while True:
                claim = self._claim_once(
                    node_id,
                    certificate_serial,
                    lease_seconds,
                    protocol_version,
                    advertised,
                    source,
                )
                if claim is not None:
                    return claim
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._available.wait(remaining)

    def _claim_once(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
        source: AgentSource | None,
    ) -> AgentClaim | None:
        with self._claim_lock, self._sessions.begin() as session:
            identity = self._lock_identity(session, node_id, certificate_serial)
            now = self._clock()
            if identity is None or not self._identity_is_active(*identity, now):
                return None
            node, certificate = identity
            self._consume_contact(session, source, node, certificate)
            self._record_contact(node, now, protocol_version, capabilities)
            expired_attempt = select(AgentOperationAttempt.id).where(
                AgentOperationAttempt.operation_id == StoredOperation.id,
                AgentOperationAttempt.attempt == StoredOperation.current_attempt,
                AgentOperationAttempt.state == "running",
                AgentOperationAttempt.lease_deadline <= now,
            ).exists()
            statement = (
                select(StoredOperation)
                .where(
                    StoredOperation.node_id == node_id,
                    or_(
                        and_(
                            StoredOperation.state == "queued",
                            StoredOperation.current_attempt == 0,
                        ),
                        and_(
                            StoredOperation.state == "running",
                            expired_attempt,
                        ),
                        and_(
                            StoredOperation.state == "waiting-for-operator",
                            StoredOperation.retry_disposition == _RETRY_DISPOSITION,
                            StoredOperation.retry_disposition_attempt == StoredOperation.current_attempt,
                        ),
                    ),
                )
                .order_by(StoredOperation.created_at, StoredOperation.id)
                .with_for_update(of=StoredOperation, skip_locked=True)
                .limit(1)
            )
            operation = session.scalars(statement).first()
            if operation is None:
                return None
            if operation.current_attempt:
                previous = session.scalar(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == operation.id,
                        AgentOperationAttempt.attempt == operation.current_attempt,
                    ).with_for_update(of=AgentOperationAttempt)
                )
                if previous is not None:
                    previous.state = "expired"
            if operation.state == "running" and operation.kind not in _SAFE_AUTOMATIC_RECLAIM:
                operation.state = "waiting-for-operator"
                operation.retry_disposition = None
                operation.retry_disposition_attempt = None
                operation.updated_at = now
                if not self._project_unsafe_expiry(session, operation, now):
                    self._aggregate_parent(session, operation.parent_job_id)
                return None
            operation.current_attempt += 1
            operation.state = "running"
            operation.updated_at = now
            deadline = now + timedelta(seconds=lease_seconds)
            attempt = AgentOperationAttempt(
                operation_id=operation.id,
                attempt=operation.current_attempt,
                fence=str(uuid.uuid4()),
                lease_deadline=deadline,
                agent_certificate_serial=certificate_serial,
                state="running",
            )
            session.add(attempt)
            return AgentClaim(
                schema_version=1,
                job_id=operation.parent_job_id,
                operation_id=operation.id,
                attempt=attempt.attempt,
                fence=attempt.fence,
                node_id=operation.node_id,
                operation=AgentOperation(operation.kind),
                base_commit=operation.base_commit,
                payload_digest=operation.payload_digest,
                payload=operation.payload,
                deadline=deadline,
            )

    def heartbeat(
        self,
        fence: AgentFence,
        progress: Mapping[str, object],
        lease_seconds: int,
        *,
        source: AgentSource | None = None,
    ) -> AgentProgress:
        self._mark_started()
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            operation, attempt = self._active(session, fence, source=source)
            now = self._clock()
            deadline = max(
                _aware(attempt.lease_deadline),
                _aware(now) + timedelta(seconds=lease_seconds),
            )
            message = AgentProgress(
                schema_version=1,
                job_id=operation.parent_job_id,
                operation_id=operation.id,
                attempt=attempt.attempt,
                fence=attempt.fence,
                node_id=operation.node_id,
                deadline=deadline,
                progress=progress,
            )
            attempt.progress = _document(message.progress)
            attempt.lease_deadline = deadline
            operation.updated_at = now
            return message

    def succeed(self, fence: AgentFence, result: Mapping[str, object]) -> None:
        self._finish(fence, "succeeded", result=result, reason=None)

    def fail(self, fence: AgentFence, reason: str) -> None:
        self._finish(fence, "failed", result=None, reason=reason)

    def wait_for_operator(self, fence: AgentFence, reason: str) -> None:
        self._finish(fence, "waiting-for-operator", result=None, reason=reason)

    def record_result(
        self, message: AgentResult, *, source: AgentSource | None = None
    ) -> None:
        """Persist one exact agent result and consume it in the same transaction."""
        self._finish(
            message,
            message.state,
            result=message.result,
            reason=None,
            source=source,
        )

    def _finish(
        self,
        fence: AgentFence,
        state: str,
        *,
        result: Mapping[str, object] | None,
        reason: str | None,
        source: AgentSource | None = None,
    ) -> None:
        self._mark_started()
        with self._sessions.begin() as session:
            operation, attempt = self._active(session, fence, source=source)
            now = self._clock()
            if isinstance(fence, AgentResult):
                if fence.state != state or (
                    result is not None
                    and _document(fence.result) != _document(result)
                ):
                    raise ValueError("agent result does not match requested completion")
                message = fence
            else:
                canonical_result = (
                    result
                    if result is not None
                    else {"reason": self._reason(reason)}
                )
                message = AgentResult(
                    schema_version=1,
                    job_id=operation.parent_job_id,
                    operation_id=operation.id,
                    attempt=attempt.attempt,
                    fence=attempt.fence,
                    node_id=operation.node_id,
                    deadline=_aware(attempt.lease_deadline),
                    state=state,
                    result=canonical_result,
                )
            attempt.result = _document(message.result)
            if (
                result is not None
                and state == "succeeded"
                and operation.kind == AgentOperation.NODE_PROBE.value
            ):
                health = self._probe_health(message.result)
                if (
                    operation.payload
                    == {"require_active_nvidia_compute_processes": 0}
                    and health["active_nvidia_compute_processes"] != 0
                ):
                    raise ValueError("node probe compute gate is unsatisfied")
                session.add(
                    Observation(
                        node_id=operation.node_id,
                        kind="health",
                        payload=health,
                        observed_at=now,
                    )
                )
            attempt.state = state
            operation.state = state
            operation.updated_at = now
            if self._result_consumer is not None:
                self._result_consumer(session, operation, attempt, message)
            self._aggregate_parent(session, operation.parent_job_id)

    def _active(
        self,
        session: Session,
        fence: AgentFence,
        *,
        source: AgentSource | None = None,
    ) -> tuple[StoredOperation, AgentOperationAttempt]:
        token = self._fence_token(fence)
        identity_hint = session.execute(
            select(
                StoredOperation.id,
                StoredOperation.node_id,
                AgentOperationAttempt.agent_certificate_serial,
            )
            .join(
                AgentOperationAttempt,
                AgentOperationAttempt.operation_id == StoredOperation.id,
            )
            .where(AgentOperationAttempt.fence == token)
        ).one_or_none()
        if identity_hint is None:
            raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")
        operation_id, node_id, certificate_serial = identity_hint
        self._lock_reconciliation_targets(session, operation_id)
        identity = self._lock_identity(session, node_id, certificate_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")
        node, certificate = identity
        self._consume_contact(session, source, node, certificate)
        operation = session.scalar(
            select(StoredOperation)
            .where(StoredOperation.id == operation_id)
            .with_for_update(of=StoredOperation)
        )
        if operation is None:
            raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.fence == token,
                AgentOperationAttempt.operation_id == operation.id,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if (
            attempt is None
            or (not isinstance(fence, str) and operation.parent_job_id != fence.job_id)
            or (not isinstance(fence, str) and operation.id != fence.operation_id)
            or (not isinstance(fence, str) and operation.node_id != fence.node_id)
            or (not isinstance(fence, str) and operation.current_attempt != fence.attempt)
            or attempt.operation_id != operation.id
            or attempt.state != "running"
            or _aware(attempt.lease_deadline) <= _aware(now)
        ):
            raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")
        self._record_contact(node, now, None, None)
        return operation, attempt

    @staticmethod
    def _capabilities(
        capabilities: Sequence[str] | None,
    ) -> tuple[str, ...] | None:
        if capabilities is None:
            return None
        if isinstance(capabilities, (str, bytes)):
            raise TypeError("agent capabilities are invalid")
        values = tuple(capabilities)
        if (
            len(values) != len(set(values))
            or set(values) != _IMPLEMENTED_CAPABILITIES
        ):
            raise ValueError("agent capabilities are invalid")
        return tuple(sorted(values))

    @staticmethod
    def _record_contact(
        node: AgentNode,
        now: datetime,
        protocol_version: int | None,
        capabilities: tuple[str, ...] | None,
    ) -> None:
        current = None if node.last_seen_at is None else _aware(node.last_seen_at)
        observed = _aware(now)
        if current is None or observed > current:
            node.last_seen_at = observed
        if protocol_version is not None:
            node.protocol_version = protocol_version
        if capabilities is not None:
            node.capabilities = list(capabilities)

    def _consume_contact(
        self,
        session: Session,
        source: AgentSource | None,
        node: AgentNode,
        certificate: AgentCertificate,
    ) -> None:
        if source is None:
            return
        identity = source.identity
        if (
            identity.node_id != node.node_id
            or identity.certificate_serial != certificate.serial
            or identity.certificate_fingerprint != certificate.fingerprint
            or identity.verified is not True
        ):
            raise ValueError("agent contact source does not match its locked identity")
        if self._contact_consumer is None:
            raise RuntimeError("agent contact consumer is not configured")
        self._contact_consumer(session, source)

    @staticmethod
    def _lock_reconciliation_targets(
        session: Session, operation_id: str
    ) -> None:
        authority = session.execute(
            select(Job.reconciliation_id, Job.targets)
            .join(StoredOperation, StoredOperation.parent_job_id == Job.id)
            .where(StoredOperation.id == operation_id)
        ).one_or_none()
        if authority is None or authority.reconciliation_id is None:
            return
        targets = authority.targets
        if (
            not isinstance(targets, list)
            or not targets
            or len(targets) != len(set(targets))
            or not all(isinstance(node_id, str) for node_id in targets)
        ):
            raise ValueError("reconciliation parent targets are invalid")
        locked = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(targets))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
            )
        )
        if [node.node_id for node in locked] != sorted(targets):
            raise StaleAgentAttempt(
                "agent operation lease, certificate, or fence is stale"
            )

    def _project_unsafe_expiry(
        self,
        session: Session,
        operation: StoredOperation,
        now: datetime,
    ) -> bool:
        hint = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == operation.id
            )
        )
        if hint is None:
            return False
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == hint.reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        job = session.scalar(
            select(Job)
            .where(
                Job.id == operation.parent_job_id,
                Job.reconciliation_id == hint.reconciliation_id,
            )
            .with_for_update(of=Job)
        )
        projection = session.scalar(
            select(ReconciliationOperation)
            .where(ReconciliationOperation.id == hint.id)
            .with_for_update(of=ReconciliationOperation)
        )
        if reconciliation is None or job is None or projection is None:
            raise ValueError("unsafe agent expiry lacks reconciliation authority")
        reason = "mutating agent operation lease expired with uncertain outcome"
        projection.state = "waiting-for-operator"
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = reason
        job.state = "waiting-for-operator"
        job.status_reason = reason
        job.updated_at = now
        return True

    @staticmethod
    def _probe_health(result: Mapping[str, object]) -> dict[str, object]:
        if set(result) != {"status", "evidence"} or result.get("status") != "ok":
            raise ValueError("successful node probe result is invalid")
        evidence = result.get("evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "dgx_forge",
            "nvidia",
        }:
            raise ValueError("successful node probe evidence is invalid")
        health = evidence.get("dgx_forge")
        nvidia = evidence.get("nvidia")
        if (
            not isinstance(health, Mapping)
            or health.get("schema_version") != 1
            or not isinstance(nvidia, Mapping)
        ):
            raise ValueError("successful node probe evidence is invalid")
        memory = health.get("memory")
        storage = health.get("storage")
        accelerator = health.get("accelerator")
        memory_available = (
            memory.get("available_bytes") if isinstance(memory, Mapping) else None
        )
        disk_available = (
            storage.get("available_bytes") if isinstance(storage, Mapping) else None
        )
        memory_total = memory.get("total_bytes") if isinstance(memory, Mapping) else None
        disk_total = storage.get("total_bytes") if isinstance(storage, Mapping) else None
        accelerator_available = (
            accelerator.get("available")
            if isinstance(accelerator, Mapping)
            else False
        )
        raw_compute_processes = (
            accelerator.get("active_nvidia_compute_processes")
            if isinstance(accelerator, Mapping)
            else None
        )
        if (
            not isinstance(memory_available, int)
            or isinstance(memory_available, bool)
            or not 0 <= memory_available <= 2**63 - 1
            or not isinstance(disk_available, int)
            or isinstance(disk_available, bool)
            or not 0 <= disk_available <= 2**63 - 1
            or (
                memory_total is not None
                and (
                    not isinstance(memory_total, int)
                    or isinstance(memory_total, bool)
                    or not memory_available <= memory_total <= 2**63 - 1
                )
            )
            or (
                disk_total is not None
                and (
                    not isinstance(disk_total, int)
                    or isinstance(disk_total, bool)
                    or not disk_available <= disk_total <= 2**63 - 1
                )
            )
            or not isinstance(accelerator_available, bool)
        ):
            raise ValueError("successful node probe capacity is invalid")
        tools = nvidia.get("tools", {})
        if not isinstance(tools, Mapping):
            raise TypeError("successful node probe tool evidence is invalid")
        warning = any(
            not isinstance(item, Mapping) or item.get("status") != "ok"
            for item in tools.values()
        )
        status = (
            "critical"
            if accelerator_available is False
            else "warning" if warning else "healthy"
        )
        observation: dict[str, object] = {
            "status": status,
            "memory_available_bytes": memory_available,
            "disk_available_bytes": disk_available,
        }
        compute_processes = (
            raw_compute_processes
            if accelerator_available is True
            and isinstance(raw_compute_processes, int)
            and not isinstance(raw_compute_processes, bool)
            and 0 <= raw_compute_processes <= 65535
            else None
        )
        if compute_processes is None and observation["status"] == "healthy":
            observation["status"] = "warning"
        observation["active_nvidia_compute_processes"] = compute_processes
        observation["compute_occupancy"] = (
            "unknown"
            if compute_processes is None
            else "clean" if compute_processes == 0 else "active"
        )
        if memory_total is not None:
            observation["memory_total_bytes"] = memory_total
        if disk_total is not None:
            observation["disk_total_bytes"] = disk_total
        if len(canonical_message(observation)) > 1024:
            raise ValueError("node probe health observation is too large")
        return observation

    @staticmethod
    def _fence_token(fence: AgentFence) -> str:
        if isinstance(fence, str):
            return fence
        if isinstance(fence, (AgentClaim, AgentProgress, AgentResult)):
            return fence.fence
        raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")

    @staticmethod
    def _reason(reason: str | None) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failure reason is required")
        return redact_text(reason)[:1024]

    @staticmethod
    def _lock_identity(
        session: Session,
        node_id: str,
        certificate_serial: str,
    ) -> tuple[AgentNode, AgentCertificate] | None:
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == node_id)
            .with_for_update(of=AgentNode)
        )
        if node is None:
            return None
        certificate = session.scalar(
            select(AgentCertificate)
            .where(
                AgentCertificate.serial == certificate_serial,
                AgentCertificate.node_id == node_id,
            )
            .with_for_update(of=AgentCertificate)
        )
        return None if certificate is None else (node, certificate)

    @staticmethod
    def _identity_is_active(
        node: AgentNode,
        certificate: AgentCertificate,
        now: datetime,
    ) -> bool:
        return (
            node.state == "active"
            and node.revoked_at is None
            and certificate.state == "active"
            and certificate.revoked_at is None
            and _aware(certificate.not_before) <= _aware(now)
            and _aware(certificate.not_after) > _aware(now)
        )

    def _aggregate_parent(self, session: Session, parent_job_id: str) -> None:
        job = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise KeyError(parent_job_id)
        if job.reconciliation_id is not None:
            return
        operations = list(session.scalars(
            select(StoredOperation)
            .where(StoredOperation.parent_job_id == parent_job_id)
            .order_by(StoredOperation.created_at, StoredOperation.id)
        ))
        terminal = {"succeeded", "failed", "waiting-for-operator"}
        if not operations or any(operation.state not in terminal for operation in operations):
            return
        states = {operation.state for operation in operations}
        if "failed" in states:
            state = "failed"
        elif "waiting-for-operator" in states:
            state = "waiting-for-operator"
        else:
            state = "succeeded"
        job.state = state
        job.updated_at = self._clock()
        if state == "succeeded":
            job.status_reason = None
            return
        for operation in operations:
            if operation.state != state:
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            if attempt is not None and attempt.result is not None:
                reason = attempt.result.get("reason")
                if not isinstance(reason, str):
                    reason = attempt.result.get("error_code")
                if isinstance(reason, str):
                    job.status_reason = redact_text(reason)[:1024]
                    return
        job.status_reason = None
