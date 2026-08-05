"""Durable, evidence-gated execution of persisted reconciliation graphs."""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from dgx_agent_protocol import AgentResult, canonical_message
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentNode,
    AgentOperationAttempt,
    Job,
    Reconciliation,
    ReconciliationOperation,
    RoutePublication,
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


def _digest(document: object) -> str:
    return hashlib.sha256(canonical_message(document)).hexdigest()


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
        endpoint_resolver: Callable[[str], tuple[str, datetime]],
        clock: Callable[[], datetime],
        publication_lease_seconds: int = 60,
    ) -> None:
        if not 1 <= publication_lease_seconds <= 300:
            raise ValueError("reconciliation publication lease is invalid")
        self._sessions = sessions
        self._agent_jobs = agent_jobs
        self._publisher = publisher
        self._endpoint_resolver = endpoint_resolver
        self._clock = clock
        self._publication_lease_seconds = publication_lease_seconds
        # SQLite ignores row locks; PostgreSQL remains the production arbiter.
        self._tick_lock = threading.RLock()

    def attach_job(self, reconciliation_id: str, job_id: str) -> None:
        """Bind the sole durable parent job; JSON fields never grant authority."""

        with self._sessions.begin() as session:
            reconciliation, job, _graph, _document = self._locked_context(
                session, reconciliation_id, expected_job_id=job_id
            )
            if job.reconciliation_id not in {None, reconciliation.id}:
                raise ValueError("job is attached to another reconciliation")
            if job.base_commit != reconciliation.base_commit:
                raise ValueError("reconciliation job base commit does not match")
            job.reconciliation_id = reconciliation.id
            job.state = "running"
            job.updated_at = self._clock()

    def tick(self, reconciliation_id: str | None = None) -> bool:
        """Advance one durable phase and return whether work was available."""

        with self._tick_lock:
            candidate = reconciliation_id or self._candidate_id()
            if candidate is None:
                return False
            notify = False
            with self._sessions.begin() as session:
                reconciliation, job, graph, document = self._locked_context(
                    session, candidate
                )
                phase = reconciliation.current_phase
                if phase == "planned":
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
                    return True
                publication = self._publication(session, reconciliation.id)
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
                    notify = self._dispatch_primary(
                        session, reconciliation, job, graph, document
                    )
                elif phase == "accepting":
                    evidence_digest = self._accepted_evidence_digest(
                        self._projections(session, reconciliation.id, "primary")
                    )
                    publication.state = "publication-pending"
                    publication.evidence_digest = evidence_digest
                    reconciliation.current_phase = "publication-pending"
                elif phase == "publication-pending":
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
            if notify:
                self._agent_jobs.notify_available()
            return True

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
            self._wait_for_operator(reconciliation, job, reason)
            return
        self._handle_primary_failure(session, reconciliation, job, graph, node, reason)

    def request_cancel(self, reconciliation_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason is required")
        with self._sessions.begin() as session:
            reconciliation, job, graph, _document = self._locked_context(
                session, reconciliation_id
            )
            projections = self._projections(session, reconciliation.id, "primary")
            mutated = [
                row
                for row in projections
                if row.state == "accepted"
                and self._graph_node(graph, row.graph_operation_id).kind in _MUTATIONS
            ]
            reconciliation.terminal_reason = reason.strip()[:1024]
            if any(
                self._graph_node(graph, row.graph_operation_id).compensation_kind
                for row in mutated
            ):
                reconciliation.current_phase = "compensating"
                reconciliation.status = "running"
            elif mutated:
                self._wait_for_operator(
                    reconciliation, job, "cancellation requires operator recovery"
                )
            else:
                reconciliation.current_phase = "cancelled"
                reconciliation.status = "cancelled"
                job.state = "failed"
                job.status_reason = "reconciliation cancelled before mutation"
                job.updated_at = self._clock()

    def _candidate_id(self) -> str | None:
        with self._sessions() as session:
            return session.scalar(
                select(Reconciliation.id)
                .join(Job, Job.reconciliation_id == Reconciliation.id)
                .outerjoin(
                    RoutePublication,
                    RoutePublication.reconciliation_id == Reconciliation.id,
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
                        (
                            (Reconciliation.current_phase == "completed")
                            & (
                                RoutePublication.lease_expires_at
                                <= self._clock() + timedelta(seconds=30)
                            )
                        ),
                    )
                )
                .order_by(Reconciliation.created_at, Reconciliation.id)
                .limit(1)
            )

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
        nodes = list(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(graph.targets))
                .order_by(AgentNode.node_id)
                .with_for_update(of=AgentNode)
            )
        )
        if [node.node_id for node in nodes] != list(graph.targets) or any(
            node.state != "active" or node.revoked_at is not None for node in nodes
        ):
            raise ValueError("reconciliation target agent is unavailable")
        reconciliation = session.scalar(
            select(Reconciliation)
            .where(Reconciliation.id == reconciliation_id)
            .with_for_update(of=Reconciliation)
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
            address, observed_at = self._endpoint_resolver(node_id)
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
        accepted = {
            row.graph_operation_id: row.state
            for row in self._projections(session, reconciliation.id, "primary")
        }
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

    def _finish_failed(self, reconciliation: Reconciliation, job: Job) -> None:
        reconciliation.current_phase = "failed"
        reconciliation.status = "failed"
        job.state = "failed"
        job.status_reason = reconciliation.terminal_reason or "reconciliation failed"
        job.updated_at = self._clock()

    def _wait_for_operator(
        self, reconciliation: Reconciliation, job: Job, reason: str
    ) -> None:
        reconciliation.current_phase = "waiting-for-operator"
        reconciliation.status = "failed"
        reconciliation.terminal_reason = reason[:1024]
        job.state = "waiting-for-operator"
        job.status_reason = reason[:1024]
        job.updated_at = self._clock()

    @staticmethod
    def _result_reason(message: AgentResult) -> str:
        reason = message.result.get("reason")
        if not isinstance(reason, str):
            reason = message.result.get("error_code")
        return reason[:1024] if isinstance(reason, str) and reason else "agent operation failed"
