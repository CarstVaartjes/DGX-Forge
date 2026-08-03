"""Transactional, node-scoped agent operation queue with lease fencing."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from dgx_agent_protocol import AgentClaim, AgentOperation, AgentProgress, AgentResult, canonical_message
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .logging import redact_text
from .models import AgentCertificate, AgentNode, AgentOperation as StoredOperation, AgentOperationAttempt, Job

AgentFence = str | AgentClaim | AgentProgress | AgentResult


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
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        # SQLite ignores row locks. This only prevents same-service test races;
        # PostgreSQL correctness is provided by the database locks below.
        self._claim_lock = threading.RLock()

    def enqueue(
        self,
        parent_job_id: str,
        node_id: str,
        operation: str,
        base_commit: str,
        payload: Mapping[str, object],
    ) -> StoredOperation:
        now = self._clock()
        protocol_operation = AgentOperation(operation)
        validated = AgentClaim(
            schema_version=1,
            job_id=parent_job_id,
            operation_id=str(uuid.uuid4()),
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
        with self._sessions.begin() as session:
            if session.get(Job, parent_job_id) is None:
                raise KeyError(parent_job_id)
            if session.get(AgentNode, node_id) is None:
                raise KeyError(node_id)
            session.add(stored)
        return stored

    def claim(
        self,
        node_id: str,
        certificate_serial: str,
        lease_seconds: int,
    ) -> AgentClaim | None:
        if not node_id.strip() or not certificate_serial.strip() or lease_seconds <= 0:
            raise ValueError("node, certificate, and positive lease are required")
        now = self._clock()
        with self._claim_lock, self._sessions.begin() as session:
            if self._active_certificate(session, node_id, certificate_serial, now) is None:
                return None
            reclaimable = select(AgentOperationAttempt.id).where(
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
                        StoredOperation.state == "queued",
                        and_(
                            StoredOperation.state == "running",
                            reclaimable,
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
    ) -> AgentProgress:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._sessions.begin() as session:
            operation, attempt = self._active(session, fence)
            deadline = self._clock() + timedelta(seconds=lease_seconds)
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
            operation.updated_at = self._clock()
            return message

    def succeed(self, fence: AgentFence, result: Mapping[str, object]) -> None:
        self._finish(fence, "succeeded", result=result, reason=None)

    def fail(self, fence: AgentFence, reason: str) -> None:
        self._finish(fence, "failed", result=None, reason=reason)

    def wait_for_operator(self, fence: AgentFence, reason: str) -> None:
        self._finish(fence, "waiting-for-operator", result=None, reason=reason)

    def _finish(
        self,
        fence: AgentFence,
        state: str,
        *,
        result: Mapping[str, object] | None,
        reason: str | None,
    ) -> None:
        with self._sessions.begin() as session:
            operation, attempt = self._active(session, fence)
            if result is not None:
                message = AgentResult(
                    schema_version=1,
                    job_id=operation.parent_job_id,
                    operation_id=operation.id,
                    attempt=attempt.attempt,
                    fence=attempt.fence,
                    node_id=operation.node_id,
                    deadline=_aware(attempt.lease_deadline),
                    state=state,
                    result=result,
                )
                attempt.result = _document(message.result)
            else:
                safe_reason = self._reason(reason)
                attempt.result = {"reason": safe_reason}
            attempt.state = state
            operation.state = state
            operation.updated_at = self._clock()
            self._aggregate_parent(session, operation.parent_job_id)

    def _active(
        self,
        session: Session,
        fence: AgentFence,
    ) -> tuple[StoredOperation, AgentOperationAttempt]:
        token = self._fence_token(fence)
        operation_id = select(AgentOperationAttempt.operation_id).where(
            AgentOperationAttempt.fence == token,
        ).scalar_subquery()
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
        now = self._clock()
        if (
            attempt is None
            or (not isinstance(fence, str) and operation.parent_job_id != fence.job_id)
            or (not isinstance(fence, str) and operation.node_id != fence.node_id)
            or (not isinstance(fence, str) and operation.current_attempt != fence.attempt)
            or attempt.operation_id != operation.id
            or attempt.state != "running"
            or _aware(attempt.lease_deadline) <= _aware(now)
            or self._active_certificate(session, operation.node_id, attempt.agent_certificate_serial, now) is None
        ):
            raise StaleAgentAttempt("agent operation lease, certificate, or fence is stale")
        return operation, attempt

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
    def _active_certificate(
        session: Session,
        node_id: str,
        certificate_serial: str,
        now: datetime,
    ) -> AgentCertificate | None:
        return session.scalar(
            select(AgentCertificate)
            .join(AgentNode, AgentNode.node_id == AgentCertificate.node_id)
            .where(
                AgentCertificate.serial == certificate_serial,
                AgentCertificate.node_id == node_id,
                AgentCertificate.revoked_at.is_(None),
                AgentCertificate.not_before <= now,
                AgentCertificate.not_after > now,
                AgentNode.state == "active",
                AgentNode.revoked_at.is_(None),
            )
        )

    def _aggregate_parent(self, session: Session, parent_job_id: str) -> None:
        job = session.scalar(
            select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
        )
        if job is None:
            raise KeyError(parent_job_id)
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
                if isinstance(reason, str):
                    job.status_reason = reason
                    return
        job.status_reason = None
