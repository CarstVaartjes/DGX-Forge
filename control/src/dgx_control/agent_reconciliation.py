"""Durable, evidence-gated execution of persisted reconciliation graphs."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from dgx_agent_protocol import AgentResult, canonical_message
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .logging import redact_text
from .models import (
    AgentNode,
    AgentOperationAttempt,
    Job,
    Reconciliation,
    ReconciliationCancellation,
    ReconciliationOperation,
    RoutePublication,
    RoutePublicationOwner,
)
from .models import (
    AgentOperation as StoredAgentOperation,
)
from .orchestration import (
    OperationGraph,
    OperationNode,
    ReconciliationOrchestrator,
    validate_persisted_resolved_plan,
)
from .route_runtime import (
    AcceptedEndpointEvidence,
    ActivationMarker,
    RouteBundleRequest,
    endpoint_evidence_digest,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKLOAD_ACTIONS = {
    "workload.prepare": "prepare",
    "workload.start": "start",
    "workload.stop": "stop",
    "workload.health": "health",
    "workload.verify": "verify",
}
_MUTATIONS = frozenset(
    {
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.stop",
    }
)
_AGENT_CAPABILITIES = frozenset(
    {
        "node.probe",
        "release.install",
        "workload.health",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.verify",
    }
)
_ACTIVE_CANCELLATION_STATES = frozenset(
    {
        "requested",
        "withdrawal-pending",
        "withdrawn",
        "processing",
        "compensating",
    }
)


def _digest(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def ready_operation_ids(
    nodes: Sequence[OperationNode], states: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the deterministic next wave using accepted projections only."""

    accepted = {operation_id for operation_id, state in states.items() if state == "accepted"}
    pending = {
        node.operation_id
        for node in nodes
        if states.get(node.operation_id, "planned") == "planned"
        and all(dependency in accepted for dependency in node.dependencies)
    }
    return tuple(sorted(pending))


def compensation_order(
    nodes: Sequence[OperationNode], states: Mapping[str, str]
) -> tuple[str, ...]:
    """Return accepted compensatable mutations in reverse graph order."""

    return tuple(
        node.operation_id
        for node in reversed(tuple(nodes))
        if states.get(node.operation_id) == "accepted"
        and node.compensation_kind is not None
    )


def accepted_result_digests(
    kind: str,
    payload: Mapping[str, object],
    result: object,
) -> tuple[str, str]:
    """Authenticate bounded agent evidence against the exact dispatched request."""

    if not isinstance(result, Mapping) or set(result) != {"status", "evidence"}:
        raise ValueError("accepted agent result is invalid")
    if result.get("status") != "ok":
        raise ValueError("accepted agent result status is invalid")
    evidence = result.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypeError("accepted agent result evidence is invalid")
    if kind == "release.install":
        _release_evidence(payload, evidence)
    elif kind in _WORKLOAD_ACTIONS:
        _workload_evidence(kind, payload, evidence)
    elif kind == "node.probe":
        _probe_evidence(payload, evidence)
    else:
        raise ValueError("accepted agent result operation is invalid")
    return _digest(result), _digest(evidence)


def _release_evidence(
    payload: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if set(evidence) != {
        "status",
        "release_digest",
        "manifest_digest",
        "adapter_id",
    }:
        raise ValueError("release evidence is invalid")
    if evidence.get("status") not in {"installed", "already-installed"}:
        raise ValueError("release evidence status is invalid")
    if (
        evidence.get("release_digest") != payload.get("target_digest")
        or evidence.get("manifest_digest") != payload.get("oci_manifest_digest")
        or evidence.get("adapter_id") != payload.get("adapter_id")
        or not isinstance(evidence.get("release_digest"), str)
        or _DIGEST.fullmatch(evidence["release_digest"]) is None
        or not isinstance(evidence.get("manifest_digest"), str)
        or _OCI_DIGEST.fullmatch(evidence["manifest_digest"]) is None
        or not isinstance(evidence.get("adapter_id"), str)
        or not evidence["adapter_id"]
    ):
        raise ValueError("release evidence does not match the request")


def _workload_evidence(
    kind: str,
    payload: Mapping[str, object],
    evidence: Mapping[str, object],
) -> None:
    if set(evidence) != {
        "status",
        "action",
        "workload_id",
        "release_digest",
        "evidence_digest",
    }:
        raise ValueError("workload evidence is invalid")
    evidence_digest = evidence.get("evidence_digest")
    if (
        not isinstance(evidence.get("status"), str)
        or not evidence["status"]
        or evidence.get("action") != _WORKLOAD_ACTIONS[kind]
        or evidence.get("workload_id") != payload.get("workload_id")
        or evidence.get("release_digest") != payload.get("release_digest")
        or not isinstance(evidence_digest, str)
        or _DIGEST.fullmatch(evidence_digest) is None
    ):
        raise ValueError("workload evidence does not match the request")
    if kind == "workload.verify" and evidence_digest != payload.get(
        "expected_digest"
    ):
        raise ValueError("workload verify evidence digest does not match the request")


def _probe_evidence(
    payload: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if payload != {"require_active_nvidia_compute_processes": 0}:
        raise ValueError("node probe request is not an authenticated compute gate")
    health = evidence.get("dgx_forge")
    nvidia = evidence.get("nvidia")
    accelerator = health.get("accelerator") if isinstance(health, Mapping) else None
    if (
        set(evidence) != {"dgx_forge", "nvidia"}
        or not isinstance(health, Mapping)
        or health.get("schema_version") != 1
        or not isinstance(accelerator, Mapping)
        or accelerator.get("active_nvidia_compute_processes") != 0
        or not isinstance(nvidia, Mapping)
    ):
        raise ValueError("node probe compute gate evidence is invalid")


class AgentReconciliationService:
    """Advance one immutable graph using only durable, fenced agent evidence."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        agent_jobs: Any,
        publisher: Any,
        endpoint_resolver: Callable[[Session, str], tuple[str, datetime]],
        clock: Callable[[], datetime],
        publication_lease_seconds: int = 60,
        commit_eligible: Callable[[str], bool] | None = None,
        current_commit: Callable[[], str] | None = None,
    ) -> None:
        if not 1 <= publication_lease_seconds <= 300:
            raise ValueError("reconciliation publication lease is invalid")
        if (commit_eligible is None) != (current_commit is None):
            raise ValueError("reconciliation commit authority is incomplete")
        self._sessions = sessions
        self._agent_jobs = agent_jobs
        self._publisher = publisher
        self._endpoint_resolver = endpoint_resolver
        self._clock = clock
        self._publication_lease_seconds = publication_lease_seconds
        self._commit_eligible = commit_eligible
        self._current_commit = current_commit
        # SQLite ignores row locks; PostgreSQL remains the production arbiter.
        self._tick_lock = threading.RLock()

    def attach_job(self, reconciliation_id: str, job_id: str) -> None:
        """Bind the sole durable parent job; JSON fields never grant authority."""

        with self._sessions.begin() as session:
            reconciliation, job, graph, _document = self._locked_context(
                session, reconciliation_id, expected_job_id=job_id
            )
            if job.reconciliation_id not in {None, reconciliation.id}:
                raise ValueError("job is attached to another reconciliation")
            if job.base_commit != reconciliation.base_commit:
                raise ValueError("reconciliation job base commit does not match")
            self._require_active_targets(session, graph)
            authority_reason = self._continuous_authority_reason(
                session, reconciliation, graph, _document
            )
            if authority_reason is not None:
                raise ValueError(authority_reason)
            job.reconciliation_id = reconciliation.id
            job.state = "running"
            job.updated_at = self._clock()

    def tick(self, reconciliation_id: str | None = None) -> bool:
        """Advance one durable phase and return whether work was available."""

        with self._tick_lock:
            automatically_selected = reconciliation_id is None
            candidate = reconciliation_id or self._candidate_id()
            if candidate is None:
                return False
            notify = False
            advanced = False
            with self._sessions.begin() as session:
                reconciliation, job, graph, document = self._locked_context(
                    session, candidate
                )
                phase = reconciliation.current_phase
                cancellation = self._cancellation(session, reconciliation.id)
                if cancellation is not None:
                    cancellation_advanced = self._advance_cancellation(
                        session,
                        reconciliation,
                        job,
                        graph,
                        cancellation,
                    )
                    if cancellation_advanced is not None:
                        return cancellation_advanced
                if phase in {"failed", "cancelled", "waiting-for-operator"}:
                    return False
                if self._sweep_expired_mutations(
                    session, reconciliation, job, graph
                ):
                    return True
                authority_reason = self._continuous_authority_reason(
                    session, reconciliation, graph, document
                )
                if authority_reason is not None:
                    owns_publication = self._owns_publication(
                        session,
                        reconciliation,
                        may_supersede=False,
                    )
                    if (
                        owns_publication
                        and phase == "completed"
                        and self._publisher is not None
                    ):
                        publication = self._publication(session, reconciliation.id)
                        marker = self._publisher.withdraw(
                            reconciliation_id=reconciliation.id,
                            plan_digest=self._plan_digest(reconciliation),
                            targets=graph.targets,
                            reason="reconciliation authority lost",
                        )
                        self._store_marker(publication, marker, "routes-withdrawn")
                    self._quiesce_for_unavailable_target(
                        session,
                        reconciliation,
                        job,
                        graph,
                        authority_reason,
                    )
                    return True
                if not self._owns_publication(
                    session,
                    reconciliation,
                    may_supersede=phase == "planned",
                ):
                    return False
                if phase == "planned":
                    if job.base_commit != reconciliation.base_commit:
                        raise ValueError("reconciliation job base commit does not match")
                    session.add(
                        RoutePublication(
                            reconciliation_id=reconciliation.id,
                            state="withdrawal-pending",
                            generation=None,
                            plan_digest=self._plan_digest(reconciliation),
                        )
                    )
                    reconciliation.current_phase = "withdrawal-pending"
                    reconciliation.status = "running"
                    job.state = "running"
                    job.status_reason = None
                    job.updated_at = self._clock()
                    return True
                publication = self._publication(session, reconciliation.id)
                if phase == "completed" and automatically_selected:
                    expires_at = publication.lease_expires_at
                    if expires_at is None or _aware(expires_at) > _aware(
                        self._clock()
                    ) + timedelta(seconds=30):
                        return False
                if phase == "withdrawal-pending":
                    marker = self._publisher.withdraw(
                        reconciliation_id=reconciliation.id,
                        plan_digest=self._plan_digest(reconciliation),
                        targets=graph.targets,
                        reason="reconciliation maintenance",
                    )
                    self._store_marker(publication, marker, "routes-withdrawn")
                    reconciliation.current_phase = "routes-withdrawn"
                    return True
                if phase == "routes-withdrawn":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    existing = {
                        row.graph_operation_id
                        for row in self._projections(session, reconciliation.id, "primary")
                    }
                    for node in graph.nodes:
                        if node.operation_id not in existing:
                            session.add(
                                ReconciliationOperation(
                                    reconciliation_id=reconciliation.id,
                                    graph_operation_id=node.operation_id,
                                    role="primary",
                                    expected_payload_digest=node.payload_digest,
                                    state="planned",
                                )
                            )
                    reconciliation.current_phase = "dispatching"
                    return True
                if phase == "dispatching":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    notify = self._dispatch_primary(
                        session, reconciliation, job, graph, document
                    )
                elif phase == "accepting":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    evidence_digest = self._accepted_evidence_digest(
                        self._projections(session, reconciliation.id, "primary")
                    )
                    publication.state = "publication-pending"
                    publication.evidence_digest = evidence_digest
                    reconciliation.current_phase = "publication-pending"
                elif phase == "publication-pending":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable",
                        )
                        return True
                    request = self._publication_request(
                        session, reconciliation, document, publication
                    )
                    marker = self._publisher.publish(request)
                    self._store_marker(publication, marker, "completed")
                    reconciliation.current_phase = "completed"
                    reconciliation.status = "succeeded"
                    if reconciliation.completion_generation is None:
                        reconciliation.completion_generation = (
                            ReconciliationOrchestrator._next_completion_generation(
                                session
                            )
                        )
                    job.state = "succeeded"
                    job.status_reason = None
                    job.result = {
                        "reconciliation_id": reconciliation.id,
                        "plan_digest": self._plan_digest(reconciliation),
                        "bundle_digest": marker.manifest_sha256,
                    }
                    job.updated_at = self._clock()
                elif phase == "completed":
                    marker = self._publisher.withdraw(
                        reconciliation_id=reconciliation.id,
                        plan_digest=self._plan_digest(reconciliation),
                        targets=graph.targets,
                        reason="route lease renewal",
                    )
                    self._store_marker(publication, marker, "routes-withdrawn")
                    reconciliation.current_phase = "accepting"
                    reconciliation.status = "running"
                    job.state = "running"
                    job.updated_at = self._clock()
                elif phase == "compensating":
                    if not self._targets_are_active(session, graph):
                        self._quiesce_for_unavailable_target(
                            session,
                            reconciliation,
                            job,
                            graph,
                            "reconciliation target agent is unavailable during compensation",
                        )
                        return True
                    notify = self._dispatch_compensation(
                        session, reconciliation, job, graph, document
                    )
                elif phase in {
                    "failed",
                    "cancelled",
                    "waiting-for-operator",
                }:
                    return False
                else:
                    raise ValueError("reconciliation execution phase is invalid")
                advanced = notify or reconciliation.current_phase != phase
            if notify:
                self._agent_jobs.notify_available()
            return advanced

    def consume_result(
        self,
        session: Session,
        operation: StoredAgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        """Accept one exact result inside the agent result transaction."""

        hint = session.scalar(
            select(ReconciliationOperation).where(
                ReconciliationOperation.agent_operation_id == operation.id
            )
        )
        if hint is None:
            return
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == hint.reconciliation_id)
            .with_for_update(of=Reconciliation)
        )
        if reconciliation is None:
            raise KeyError(hint.reconciliation_id)
        job = session.scalar(
            select(Job)
            .where(
                Job.id == operation.parent_job_id,
                Job.reconciliation_id == reconciliation.id,
            )
            .with_for_update(of=Job)
        )
        projection = session.scalar(
            select(ReconciliationOperation)
            .where(ReconciliationOperation.id == hint.id)
            .with_for_update(of=ReconciliationOperation)
        )
        if job is None or projection is None:
            raise ValueError("agent result lacks its reconciliation projection")
        graph, document = self._validated_plan(reconciliation)
        node = self._graph_node(graph, projection.graph_operation_id)
        payload = self._operation_payload(document, node.operation_id)
        kind = node.kind
        if projection.role == "compensation":
            if node.compensation_kind is None:
                raise ValueError("reconciliation compensation is not graph-authorized")
            kind = node.compensation_kind
            payload = self._compensation_payload(payload)
        self._validate_operation_binding(
            operation, attempt, message, reconciliation, job, projection, node, kind, payload
        )
        expected_phase = (
            "compensating" if projection.role == "compensation" else "dispatching"
        )
        if reconciliation.current_phase != expected_phase:
            raise ValueError("agent result is invalid for reconciliation phase")
        authority_reason = self._continuous_authority_reason(
            session, reconciliation, graph, document
        )
        if authority_reason is not None:
            terminal = (
                "waiting-for-operator"
                if operation.kind in _MUTATIONS
                else "failed"
            )
            attempt.state = terminal
            operation.state = terminal
            operation.updated_at = self._clock()
            projection.state = terminal
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "primary"),
            )
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "compensation"),
            )
            self._wait_for_operator(reconciliation, job, authority_reason)
            return
        now = self._clock()
        if message.state == "succeeded":
            result_digest, evidence_digest = accepted_result_digests(
                kind, payload, message.result
            )
            projection.result_digest = result_digest
            projection.evidence_digest = evidence_digest
            projection.accepted_at = now
            projection.state = (
                "compensated" if projection.role == "compensation" else "accepted"
            )
            return
        projection.state = (
            "waiting-for-operator"
            if message.state == "waiting-for-operator"
            else "failed"
        )
        projection.result_digest = _digest(message.result)
        reason = self._result_reason(message)
        if projection.role == "compensation" or message.state == "waiting-for-operator":
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "primary"),
            )
            self._quiesce_pending(
                session,
                reconciliation,
                graph,
                self._projections(session, reconciliation.id, "compensation"),
            )
            self._wait_for_operator(reconciliation, job, reason)
            return
        self._handle_primary_failure(session, reconciliation, job, graph, node, reason)

    def request_cancel(self, reconciliation_id: str, reason: str) -> None:
        self.enqueue_cancel(
            reconciliation_id,
            reason,
            actor="internal-cancellation-adapter",
            request_id=str(uuid.uuid4()),
        )
        for _ in range(3):
            if not self.tick(reconciliation_id):
                break

    def enqueue_cancel(
        self,
        reconciliation_id: str,
        reason: str,
        *,
        actor: str,
        request_id: str,
    ) -> ReconciliationCancellation:
        """Commit idempotent cancellation intent before any external effect."""

        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(actor, str)
            or not actor.strip()
        ):
            raise ValueError("cancellation reason and actor are required")
        try:
            canonical_request_id = str(uuid.UUID(request_id))
        except (AttributeError, TypeError, ValueError):
            raise ValueError("cancellation request ID is invalid") from None
        if canonical_request_id != request_id:
            raise ValueError("cancellation request ID is invalid")
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ReconciliationCancellation)
                .where(
                    ReconciliationCancellation.reconciliation_id
                    == reconciliation_id
                )
                .with_for_update(of=ReconciliationCancellation)
            )
            if existing is not None:
                return existing
            reconciliation, _job, _graph, _document = self._locked_context(
                session, reconciliation_id
            )
            if reconciliation.current_phase in {"failed", "cancelled"}:
                raise ValueError("reconciliation is terminal")
            now = self._clock()
            cancellation = ReconciliationCancellation(
                reconciliation_id=reconciliation.id,
                state="requested",
                reason=self._safe_reason(reason.strip()),
                actor=actor.strip()[:200],
                request_id=request_id,
                requested_at=now,
                updated_at=now,
            )
            session.add(cancellation)
            session.flush()
            return cancellation

    def _advance_cancellation(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        cancellation: ReconciliationCancellation,
    ) -> bool | None:
        now = self._clock()
        if cancellation.state == "requested":
            owns_publication = self._owns_publication(
                session,
                reconciliation,
                may_supersede=False,
            )
            cancellation.state = (
                "withdrawal-pending"
                if reconciliation.current_phase == "completed" and owns_publication
                else "processing"
            )
            cancellation.updated_at = now
            return True
        if cancellation.state == "withdrawal-pending":
            owns_publication = self._owns_publication(
                session,
                reconciliation,
                may_supersede=False,
            )
            if owns_publication:
                if self._publisher is None:
                    raise RuntimeError("route publisher is unavailable")
                publication = self._publication(session, reconciliation.id)
                marker = self._publisher.withdraw(
                    reconciliation_id=reconciliation.id,
                    plan_digest=self._plan_digest(reconciliation),
                    targets=graph.targets,
                    reason="reconciliation cancellation",
                )
                self._store_marker(publication, marker, "routes-withdrawn")
                cancellation.state = "withdrawn"
            else:
                cancellation.state = "processing"
            cancellation.updated_at = now
            return True
        if cancellation.state == "withdrawn":
            cancellation.state = "processing"
            cancellation.updated_at = now
            self._apply_cancellation(
                session, reconciliation, job, graph, cancellation
            )
            return True
        if cancellation.state == "processing":
            self._apply_cancellation(
                session, reconciliation, job, graph, cancellation
            )
            return True
        if cancellation.state == "compensating":
            if reconciliation.current_phase == "failed":
                cancellation.state = "completed"
            elif reconciliation.current_phase == "waiting-for-operator":
                cancellation.state = "waiting-for-operator"
            else:
                return None
            cancellation.updated_at = now
            return True
        return None

    def _apply_cancellation(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        cancellation: ReconciliationCancellation,
    ) -> None:
        projections = self._projections(session, reconciliation.id, "primary")
        compensations = self._projections(
            session, reconciliation.id, "compensation"
        )
        if reconciliation.current_phase == "waiting-for-operator":
            self._quiesce_pending(session, reconciliation, graph, projections)
            self._quiesce_pending(session, reconciliation, graph, compensations)
            cancellation.state = "waiting-for-operator"
            cancellation.updated_at = self._clock()
            return
        unsafe_mutation = any(
            self._graph_node(graph, row.graph_operation_id).kind in _MUTATIONS
            and row.state
            in {
                "failed",
                "running",
                "succeeded",
                "uncertain",
                "waiting-for-operator",
            }
            for row in projections
        ) or any(
            row.state
            in {
                "failed",
                "running",
                "succeeded",
                "uncertain",
                "waiting-for-operator",
            }
            for row in compensations
        )
        uncertain = self._quiesce_pending(
            session, reconciliation, graph, projections
        )
        uncertain = (
            self._quiesce_pending(
                session, reconciliation, graph, compensations
            )
            or uncertain
        )
        mutated = [
            row
            for row in projections
            if row.state == "accepted"
            and self._graph_node(graph, row.graph_operation_id).kind in _MUTATIONS
        ]
        reconciliation.terminal_reason = cancellation.reason
        if uncertain or unsafe_mutation:
            self._wait_for_operator(
                reconciliation,
                job,
                "cancellation interrupted a running mutation",
            )
            cancellation.state = "waiting-for-operator"
        elif any(
            self._graph_node(graph, row.graph_operation_id).compensation_kind
            for row in mutated
        ):
            reconciliation.current_phase = "compensating"
            reconciliation.status = "running"
            job.state = "running"
            job.updated_at = self._clock()
            cancellation.state = "compensating"
        elif mutated:
            self._wait_for_operator(
                reconciliation, job, "cancellation requires operator recovery"
            )
            cancellation.state = "waiting-for-operator"
        else:
            reconciliation.current_phase = "cancelled"
            reconciliation.status = "cancelled"
            job.state = "failed"
            job.status_reason = "reconciliation cancelled before mutation"
            job.updated_at = self._clock()
            cancellation.state = "completed"
        cancellation.updated_at = self._clock()

    def _candidate_id(self) -> str | None:
        with self._sessions() as session:
            return session.scalar(
                select(Reconciliation.id)
                .join(Job, Job.reconciliation_id == Reconciliation.id)
                .outerjoin(
                    RoutePublication,
                    RoutePublication.reconciliation_id == Reconciliation.id,
                )
                .outerjoin(
                    ReconciliationCancellation,
                    ReconciliationCancellation.reconciliation_id
                    == Reconciliation.id,
                )
                .where(
                    or_(
                        Reconciliation.current_phase.in_(
                            {
                                "planned",
                                "withdrawal-pending",
                                "routes-withdrawn",
                                "dispatching",
                                "accepting",
                                "publication-pending",
                                "compensating",
                            }
                        ),
                        Reconciliation.current_phase == "completed",
                        ReconciliationCancellation.state.in_(
                            _ACTIVE_CANCELLATION_STATES
                        ),
                    )
                )
                .order_by(Reconciliation.created_at.desc(), Reconciliation.id.desc())
                .limit(1)
            )

    def _owns_publication(
        self,
        session: Session,
        reconciliation: Reconciliation,
        *,
        may_supersede: bool,
    ) -> bool:
        """Lock and enforce the sole global activation-marker owner."""

        statement = (
            select(RoutePublicationOwner)
            .where(RoutePublicationOwner.singleton_id == 1)
            .with_for_update(of=RoutePublicationOwner)
        )
        owner = session.scalar(statement)
        if owner is None:
            try:
                with session.begin_nested():
                    session.add(
                        RoutePublicationOwner(
                            singleton_id=1,
                            reconciliation_id=None,
                            owner_generation=0,
                        )
                    )
                    session.flush()
            except IntegrityError:
                pass
            owner = session.scalar(statement)
        if owner is None:
            raise RuntimeError("route publication owner is unavailable")
        if owner.reconciliation_id is None:
            latest_completed = session.scalar(
                select(Reconciliation.id)
                .where(
                    Reconciliation.status == "succeeded",
                    Reconciliation.current_phase == "completed",
                    Reconciliation.completion_generation.is_not(None),
                )
                .order_by(
                    Reconciliation.completion_generation.desc(),
                    Reconciliation.id.desc(),
                )
                .limit(1)
            )
            owner.reconciliation_id = latest_completed
        if owner.reconciliation_id == reconciliation.id:
            return True
        current = (
            None
            if owner.reconciliation_id is None
            else session.get(Reconciliation, owner.reconciliation_id)
        )
        candidate_order = (_aware(reconciliation.created_at), reconciliation.id)
        current_order = (
            None
            if current is None
            else (_aware(current.created_at), current.id)
        )
        if current_order is not None and (
            not may_supersede or candidate_order <= current_order
        ):
            return False
        owner.reconciliation_id = reconciliation.id
        owner.owner_generation += 1
        owner.updated_at = self._clock()
        return True

    def _locked_context(
        self,
        session: Session,
        reconciliation_id: str,
        *,
        expected_job_id: str | None = None,
    ) -> tuple[Reconciliation, Job, OperationGraph, Mapping[str, object]]:
        preview = session.get(Reconciliation, reconciliation_id)
        if preview is None:
            raise KeyError(reconciliation_id)
        graph, _document = self._validated_plan(preview)
        _locked_nodes = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
            )
        )
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == reconciliation_id)
            .with_for_update(of=Reconciliation)
            .execution_options(populate_existing=True)
        )
        assert reconciliation is not None
        graph, document = self._validated_plan(reconciliation)
        job_query = select(Job).where(
            Job.id == expected_job_id
            if expected_job_id is not None
            else Job.reconciliation_id == reconciliation.id
        )
        job = session.scalar(job_query.with_for_update(of=Job))
        if job is None:
            raise ValueError("reconciliation has no durable parent job")
        if expected_job_id is None and job.reconciliation_id != reconciliation.id:
            raise ValueError("reconciliation parent link is invalid")
        return reconciliation, job, graph, document

    @staticmethod
    def _targets_are_active(session: Session, graph: OperationGraph) -> bool:
        nodes = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
            )
        )
        return [node.node_id for node in nodes] == list(graph.targets) and all(
            node.state == "active" and node.revoked_at is None for node in nodes
        )

    def _continuous_authority_reason(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> str | None:
        if self._commit_eligible is None or self._current_commit is None:
            return None
        try:
            if not self._commit_eligible(reconciliation.base_commit):
                return "reconciliation commit is no longer eligible"
            if self._current_commit() != reconciliation.base_commit:
                return "reconciliation commit is no longer current"
        except (OSError, RuntimeError, TypeError, ValueError):
            return "reconciliation commit eligibility is unavailable"
        protocol = document.get("agent_protocol_range")
        if (
            not isinstance(protocol, list)
            or len(protocol) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in protocol
            )
        ):
            return "reconciliation protocol authority is invalid"
        nodes = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
            )
        )
        if [node.node_id for node in nodes] != list(graph.targets):
            return "reconciliation target set is unavailable"
        if any(
            node.state != "active"
            or node.revoked_at is not None
            or not isinstance(node.protocol_version, int)
            or isinstance(node.protocol_version, bool)
            or not protocol[0] <= node.protocol_version <= protocol[1]
            or not isinstance(node.capabilities, list)
            or set(node.capabilities) != _AGENT_CAPABILITIES
            for node in nodes
        ):
            return "reconciliation target agent is incompatible"
        try:
            for node_id in graph.targets:
                address, observed_at = self._endpoint_resolver(session, node_id)
                if (
                    not isinstance(address, str)
                    or not address
                    or not isinstance(observed_at, datetime)
                ):
                    return "reconciliation management address is invalid"
        except (OSError, RuntimeError, TypeError, ValueError):
            return "reconciliation management address is unavailable"
        return None

    @classmethod
    def _require_active_targets(
        cls, session: Session, graph: OperationGraph
    ) -> None:
        if not cls._targets_are_active(session, graph):
            raise ValueError("reconciliation target agent is unavailable")

    @staticmethod
    def _validated_plan(
        reconciliation: Reconciliation,
    ) -> tuple[OperationGraph, Mapping[str, object]]:
        return validate_persisted_resolved_plan(
            reconciliation_id=reconciliation.id,
            base_commit=reconciliation.base_commit,
            graph_document=reconciliation.graph,
            graph_digest=reconciliation.graph_digest,
            plan_digest=reconciliation.plan_digest,
            resolved_document=reconciliation.resolved_plan,
            route_withdrawal_generation=reconciliation.route_withdrawal_generation,
        )

    @staticmethod
    def _plan_digest(reconciliation: Reconciliation) -> str:
        if not isinstance(reconciliation.plan_digest, str):
            raise TypeError("reconciliation lacks an immutable plan digest")
        return reconciliation.plan_digest

    @staticmethod
    def _publication(session: Session, reconciliation_id: str) -> RoutePublication:
        publication = session.scalar(
            select(RoutePublication)
            .where(RoutePublication.reconciliation_id == reconciliation_id)
            .with_for_update(of=RoutePublication)
        )
        if publication is None:
            raise ValueError("reconciliation route withdrawal is not durable")
        return publication

    @staticmethod
    def _cancellation(
        session: Session, reconciliation_id: str
    ) -> ReconciliationCancellation | None:
        return session.scalar(
            select(ReconciliationCancellation)
            .where(
                ReconciliationCancellation.reconciliation_id
                == reconciliation_id
            )
            .with_for_update(of=ReconciliationCancellation)
        )

    @staticmethod
    def _projections(
        session: Session, reconciliation_id: str, role: str
    ) -> list[ReconciliationOperation]:
        return list(
            session.scalars(
                select(ReconciliationOperation)
                .where(
                    ReconciliationOperation.reconciliation_id == reconciliation_id,
                    ReconciliationOperation.role == role,
                )
                .order_by(ReconciliationOperation.graph_operation_id)
                .with_for_update(of=ReconciliationOperation)
            )
        )

    def _dispatch_primary(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> bool:
        projections = self._projections(session, reconciliation.id, "primary")
        states = {row.graph_operation_id: row.state for row in projections}
        if len(projections) != len(graph.nodes):
            raise ValueError("reconciliation execution projection is incomplete")
        if all(state == "accepted" for state in states.values()):
            reconciliation.current_phase = "accepting"
            return False
        ready = ready_operation_ids(graph.nodes, states)
        by_id = {row.graph_operation_id: row for row in projections}
        for operation_id in ready:
            node = self._graph_node(graph, operation_id)
            payload = self._operation_payload(document, operation_id)
            agent_operation_id = str(uuid.uuid4())
            stored = self._agent_jobs.enqueue_in_session(
                session,
                job.id,
                node.node_id,
                node.kind,
                reconciliation.base_commit,
                payload,
                operation_id=agent_operation_id,
            )
            projection = by_id[operation_id]
            projection.agent_operation_id = stored.id
            projection.state = "queued"
        return bool(ready)

    def _dispatch_compensation(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        document: Mapping[str, object],
    ) -> bool:
        primary = self._projections(session, reconciliation.id, "primary")
        primary_states = {row.graph_operation_id: row.state for row in primary}
        ordered = compensation_order(graph.nodes, primary_states)
        existing = {
            row.graph_operation_id: row
            for row in self._projections(session, reconciliation.id, "compensation")
        }
        if not ordered:
            self._finish_failed(reconciliation, job)
            return False
        for operation_id in ordered:
            row = existing.get(operation_id)
            if row is not None:
                if row.state in {"queued", "running"}:
                    return False
                if row.state == "compensated":
                    continue
                if row.state in {"failed", "uncertain", "waiting-for-operator"}:
                    self._wait_for_operator(
                        reconciliation, job, "reconciliation compensation is incomplete"
                    )
                    return False
            node = self._graph_node(graph, operation_id)
            payload = self._compensation_payload(
                self._operation_payload(document, operation_id)
            )
            expected = _digest(payload)
            row = ReconciliationOperation(
                reconciliation_id=reconciliation.id,
                graph_operation_id=operation_id,
                role="compensation",
                expected_payload_digest=expected,
                state="planned",
                compensated_graph_operation_id=operation_id,
            )
            session.add(row)
            session.flush()
            agent_operation_id = str(uuid.uuid4())
            stored = self._agent_jobs.enqueue_in_session(
                session,
                job.id,
                node.node_id,
                node.compensation_kind,
                reconciliation.base_commit,
                payload,
                operation_id=agent_operation_id,
            )
            row.agent_operation_id = stored.id
            row.state = "queued"
            return True
        self._finish_failed(reconciliation, job)
        return False

    def _publication_request(
        self,
        session: Session,
        reconciliation: Reconciliation,
        document: Mapping[str, object],
        publication: RoutePublication,
    ) -> RouteBundleRequest:
        if not isinstance(publication.evidence_digest, str):
            raise TypeError("accepted reconciliation evidence set is unavailable")
        routes = document.get("routes")
        if not isinstance(routes, Mapping) or not routes:
            raise ValueError("accepted reconciliation routes are unavailable")
        projections = self._projections(session, reconciliation.id, "primary")
        by_operation = {row.graph_operation_id: row for row in projections}
        endpoints: dict[str, AcceptedEndpointEvidence] = {}
        for raw in routes.values():
            if not isinstance(raw, Mapping):
                raise TypeError("accepted route is invalid")
            node_id = raw.get("entrypoint_node_id")
            workload_id = raw.get("workload_id")
            if not isinstance(node_id, str) or not isinstance(workload_id, str):
                raise TypeError("accepted route entrypoint is invalid")
            operation_id = f"{workload_id}:{node_id}:workload.verify"
            projection = by_operation.get(operation_id)
            if (
                projection is None
                or projection.state != "accepted"
                or not isinstance(projection.evidence_digest, str)
            ):
                raise ValueError("accepted route lacks exact verify evidence")
            address, observed_at = self._endpoint_resolver(session, node_id)
            endpoint_digest = endpoint_evidence_digest(
                node_id=node_id,
                address=address,
                observed_at=observed_at,
                operation_id=operation_id,
                verify_evidence_digest=projection.evidence_digest,
            )
            endpoints[node_id] = AcceptedEndpointEvidence(
                node_id,
                address,
                observed_at,
                operation_id,
                projection.evidence_digest,
                endpoint_digest,
            )
        return RouteBundleRequest(
            reconciliation.id,
            self._plan_digest(reconciliation),
            publication.evidence_digest,
            routes,
            endpoints,
            self._clock() + timedelta(seconds=self._publication_lease_seconds),
        )

    @staticmethod
    def _accepted_evidence_digest(
        projections: Sequence[ReconciliationOperation],
    ) -> str:
        if not projections or any(
            row.state != "accepted"
            or not isinstance(row.result_digest, str)
            or not isinstance(row.evidence_digest, str)
            for row in projections
        ):
            raise ValueError("reconciliation operation evidence is incomplete")
        return _digest(
            [
                {
                    "operation_id": row.graph_operation_id,
                    "result_digest": row.result_digest,
                    "evidence_digest": row.evidence_digest,
                }
                for row in sorted(projections, key=lambda item: item.graph_operation_id)
            ]
        )

    @staticmethod
    def _store_marker(
        publication: RoutePublication, marker: ActivationMarker, state: str
    ) -> None:
        document = asdict(marker)
        publication.state = state
        publication.generation = marker.generation
        publication.plan_digest = marker.plan_digest
        publication.evidence_digest = marker.evidence_set_digest
        publication.route_digest = marker.routes_sha256
        publication.litellm_digest = marker.litellm_sha256
        publication.bundle_digest = marker.manifest_sha256
        publication.activation_marker = document
        publication.activation_marker_digest = marker.digest
        publication.lease_issued_at = datetime.fromisoformat(marker.issued_at)
        publication.lease_expires_at = datetime.fromisoformat(marker.expires_at)

    @staticmethod
    def _operation_payload(
        document: Mapping[str, object], operation_id: str
    ) -> Mapping[str, object]:
        payloads = document.get("operation_payloads")
        if not isinstance(payloads, Mapping):
            raise TypeError("reconciliation operation payloads are invalid")
        payload = payloads.get(operation_id)
        if not isinstance(payload, Mapping):
            raise TypeError("reconciliation operation payload is invalid")
        return payload

    @staticmethod
    def _graph_node(graph: OperationGraph, operation_id: str) -> OperationNode:
        for node in graph.nodes:
            if node.operation_id == operation_id:
                return node
        raise ValueError("execution projection operation is absent from the graph")

    @staticmethod
    def _compensation_payload(
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        required = {"schema_version", "workload_id", "release_digest", "adapter_id"}
        if not required.issubset(payload):
            raise ValueError("workload compensation payload is incomplete")
        return {key: payload[key] for key in sorted(required)}

    @staticmethod
    def _validate_operation_binding(
        operation: StoredAgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
        reconciliation: Reconciliation,
        job: Job,
        projection: ReconciliationOperation,
        node: OperationNode,
        kind: str,
        payload: Mapping[str, object],
    ) -> None:
        if (
            projection.agent_operation_id != operation.id
            or projection.expected_payload_digest != _digest(payload)
            or operation.parent_job_id != job.id
            or operation.node_id != node.node_id
            or operation.kind != kind
            or operation.base_commit != reconciliation.base_commit
            or operation.payload_digest != projection.expected_payload_digest
            or operation.payload != payload
            or message.job_id != job.id
            or message.operation_id != operation.id
            or message.node_id != operation.node_id
            or message.attempt != attempt.attempt
            or message.fence != attempt.fence
            or message.state not in {"succeeded", "failed", "waiting-for-operator"}
        ):
            raise ValueError("agent result does not match its reconciliation operation")

    def _handle_primary_failure(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        failed_node: OperationNode,
        reason: str,
    ) -> None:
        projections = self._projections(session, reconciliation.id, "primary")
        if self._quiesce_pending(session, reconciliation, graph, projections):
            self._wait_for_operator(
                reconciliation,
                job,
                "a sibling mutation was running when reconciliation failed",
            )
            return
        accepted = {row.graph_operation_id: row.state for row in projections}
        compensatable = compensation_order(graph.nodes, accepted)
        if failed_node.kind in {"workload.start", "workload.health", "workload.verify"} and compensatable:
            reconciliation.current_phase = "compensating"
            reconciliation.status = "running"
            reconciliation.terminal_reason = reason
        elif failed_node.kind in _MUTATIONS:
            self._wait_for_operator(reconciliation, job, reason)
        else:
            reconciliation.current_phase = "failed"
            reconciliation.status = "failed"
            reconciliation.terminal_reason = reason
            job.state = "failed"
            job.status_reason = reason
            job.updated_at = self._clock()

    def _quiesce_pending(
        self,
        session: Session,
        reconciliation: Reconciliation,
        graph: OperationGraph,
        projections: Sequence[ReconciliationOperation],
    ) -> bool:
        """Fence unresolved work while target Node locks serialize agent traffic."""

        uncertain_mutation = False
        now = self._clock()
        for projection in projections:
            if projection.state == "planned":
                projection.state = "failed"
                continue
            if projection.state != "queued" or projection.agent_operation_id is None:
                continue
            operation = session.scalar(
                select(StoredAgentOperation)
                .where(StoredAgentOperation.id == projection.agent_operation_id)
                .with_for_update(of=StoredAgentOperation)
            )
            if operation is None:
                raise ValueError("reconciliation operation projection is incomplete")
            node = self._graph_node(graph, projection.graph_operation_id)
            if operation.state == "queued":
                operation.state = "failed"
                operation.updated_at = now
                projection.state = "failed"
                continue
            if operation.state != "running":
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            if attempt is None or attempt.state != "running":
                raise ValueError("running reconciliation operation lacks its attempt")
            if node.kind in _MUTATIONS:
                operation.state = "waiting-for-operator"
                attempt.state = "waiting-for-operator"
                projection.state = "waiting-for-operator"
                uncertain_mutation = True
            else:
                operation.state = "failed"
                attempt.state = "failed"
                projection.state = "failed"
            operation.updated_at = now
        return uncertain_mutation

    def _sweep_expired_mutations(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
    ) -> bool:
        """Project expired mutation leases without requiring another claim."""

        primary = self._projections(session, reconciliation.id, "primary")
        compensation = self._projections(
            session, reconciliation.id, "compensation"
        )
        now = self._clock()
        expired = False
        for projection in (*primary, *compensation):
            if projection.state != "queued" or projection.agent_operation_id is None:
                continue
            operation = session.scalar(
                select(StoredAgentOperation)
                .where(StoredAgentOperation.id == projection.agent_operation_id)
                .with_for_update(of=StoredAgentOperation)
            )
            if (
                operation is None
                or operation.state != "running"
                or operation.kind not in _MUTATIONS
            ):
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            if (
                attempt is None
                or attempt.state != "running"
                or _aware(attempt.lease_deadline) > _aware(now)
            ):
                continue
            attempt.state = "expired"
            operation.state = "waiting-for-operator"
            operation.retry_disposition = None
            operation.retry_disposition_attempt = None
            operation.updated_at = now
            projection.state = "waiting-for-operator"
            expired = True
        if not expired:
            return False
        self._quiesce_pending(session, reconciliation, graph, primary)
        self._quiesce_pending(session, reconciliation, graph, compensation)
        self._wait_for_operator(
            reconciliation,
            job,
            "mutating agent operation lease expired with uncertain outcome",
        )
        return True

    def _quiesce_for_unavailable_target(
        self,
        session: Session,
        reconciliation: Reconciliation,
        job: Job,
        graph: OperationGraph,
        reason: str,
    ) -> None:
        self._quiesce_pending(
            session,
            reconciliation,
            graph,
            self._projections(session, reconciliation.id, "primary"),
        )
        self._quiesce_pending(
            session,
            reconciliation,
            graph,
            self._projections(session, reconciliation.id, "compensation"),
        )
        self._wait_for_operator(reconciliation, job, reason)

    def _finish_failed(self, reconciliation: Reconciliation, job: Job) -> None:
        reconciliation.current_phase = "failed"
        reconciliation.status = "failed"
        job.state = "failed"
        job.status_reason = reconciliation.terminal_reason or "reconciliation failed"
        job.updated_at = self._clock()

    def _wait_for_operator(
        self, reconciliation: Reconciliation, job: Job, reason: str
    ) -> None:
        safe_reason = self._safe_reason(reason)
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = safe_reason
        job.state = "waiting-for-operator"
        job.status_reason = safe_reason
        job.updated_at = self._clock()

    @staticmethod
    def _safe_reason(reason: str) -> str:
        return redact_text(reason)[:1024]

    @classmethod
    def _result_reason(cls, message: AgentResult) -> str:
        reason = message.result.get("reason")
        if not isinstance(reason, str):
            reason = message.result.get("error_code")
        return cls._safe_reason(reason if isinstance(reason, str) and reason else "agent operation failed")


def bind_reconciliation_result_consumer(
    sessions: sessionmaker[Session],
    *,
    operations: Any,
    presence: Any,
    clock: Callable[[], datetime],
    maximum_presence_age_seconds: int = 300,
    commit_eligible: Callable[[str], bool] | None = None,
    current_commit: Callable[[], str] | None = None,
) -> AgentReconciliationService:
    """Bind the API's result queue to the same durable execution projection."""

    if not 1 <= maximum_presence_age_seconds <= 300:
        raise ValueError("reconciliation presence age is invalid")

    def endpoint(session: Session, node_id: str) -> tuple[str, datetime]:
        observation = presence.latest_in_session(
            session,
            node_id,
            maximum_age_seconds=maximum_presence_age_seconds,
        )
        return observation.address, observation.observed_at

    service = AgentReconciliationService(
        sessions,
        agent_jobs=operations,
        publisher=None,
        endpoint_resolver=endpoint,
        clock=clock,
        commit_eligible=commit_eligible,
        current_commit=current_commit,
    )
    operations.set_result_consumer(service.consume_result)
    return service
