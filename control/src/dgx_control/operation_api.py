"""Strict, secret-free representations for routine administrative operations."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
    Reconciliation,
    ReconciliationOperation,
    RoutePublication,
    RoutePublicationOwner,
)
from .route_runtime import verify_active_route_bundle

COMMIT_PATTERN = r"^[0-9a-f]{40}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"
NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_ACTIVE_PUBLICATION_STATES = frozenset({"completed"})
_ADMIN_OPERATION_IDS = {
    ("post", "/api/v1/agents/enrollments/grants"): "createEnrollmentGrant",
    ("get", "/api/v1/agents/enrollments"): "listAgentEnrollments",
    ("post", "/api/v1/agents/enrollments/{enrollment_id}/approve"): "approveAgentEnrollment",
    ("post", "/api/v1/agents/enrollments/{enrollment_id}/reject"): "rejectAgentEnrollment",
    ("post", "/api/v1/agents/nodes/{node_id}/revoke"): "revokeAgentNode",
    ("get", "/api/v1/fleet"): "getFleetStatus",
    ("get", "/api/v1/nodes/status"): "getNodeStatuses",
    ("get", "/api/v1/endpoints/{alias}"): "getPublishedEndpoint",
    ("get", "/api/v1/agents"): "listAgents",
    ("get", "/api/v1/repository"): "getRepository",
    ("get", "/api/v1/documents"): "listDocuments",
    ("post", "/api/v1/proposals"): "previewProposal",
    ("post", "/api/v1/changes"): "submitChange",
    ("post", "/api/v1/reconciliations/plan"): "planReconciliation",
    ("post", "/api/v1/profiles/{profile_id}/plan"): "planProfileReconciliation",
    ("post", "/api/v1/reconciliations"): "applyReconciliation",
    ("post", "/api/v1/reconciliations/{reconciliation_id}/cancel"): "cancelReconciliation",
    ("get", "/api/v1/jobs"): "listJobs",
    ("get", "/api/v1/audit"): "listAuditEvents",
    ("get", "/api/v1/jobs/{job_id}"): "getJob",
    ("post", "/api/v1/jobs/{job_id}/resume"): "resumeJob",
    ("get", "/api/v1/jobs/{job_id}/logs"): "listJobLogs",
    ("get", "/api/v1/jobs/{job_id}/logs/{digest}"): "getJobLog",
}
_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})


class OperationProjectionError(RuntimeError):
    """Durable operation state cannot be safely projected."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyBody(StrictModel):
    """A body type used only where an explicit empty JSON object is allowed."""


class BoundedErrorResponse(StrictModel):
    detail: str = Field(min_length=1, max_length=256)


def bounded_error_responses(*status_codes: int) -> dict[int, dict[str, object]]:
    """Describe stable JSON errors for generated clients."""

    return {
        status_code: {"model": BoundedErrorResponse}
        for status_code in status_codes
    }


class PlanPlacements(RootModel[dict[str, list[str]]]):
    root: dict[str, list[str]] = Field(max_length=128)


class PlanQuota(StrictModel):
    requests_per_minute: int = Field(ge=1, le=100_000, strict=True)
    tokens_per_minute: int = Field(ge=1, le=100_000_000, strict=True)


class PlanRoute(StrictModel):
    workload_id: str = Field(pattern=IDENTIFIER_PATTERN)
    nodes: list[str] = Field(min_length=1, max_length=64)
    entrypoint_node_id: str = Field(pattern=NODE_PATTERN)
    scheme: Literal["http", "https"]
    port: int = Field(ge=1, le=65535, strict=True)
    path: str = Field(min_length=1, max_length=512, pattern=r"^/")
    quota: PlanQuota
    quota_digest: str = Field(pattern=DIGEST_PATTERN)


class PlanRoutes(RootModel[dict[str, PlanRoute]]):
    root: dict[str, PlanRoute] = Field(max_length=128)


class PlanReleaseRequest(StrictModel):
    schema_version: Literal[1]
    target_name: str = Field(pattern=IDENTIFIER_PATTERN)
    oci_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_digest: str = Field(pattern=DIGEST_PATTERN)
    provenance_digest: str = Field(pattern=DIGEST_PATTERN)
    adapter_id: str = Field(pattern=IDENTIFIER_PATTERN)


class PlanWorkloadRequest(StrictModel):
    schema_version: Literal[1]
    workload_id: str = Field(pattern=IDENTIFIER_PATTERN)
    release_digest: str = Field(pattern=DIGEST_PATTERN)
    adapter_id: str = Field(pattern=IDENTIFIER_PATTERN)


class PlanPrepareRequest(PlanWorkloadRequest):
    profile_digest: str = Field(pattern=DIGEST_PATTERN)


class PlanStartRequest(PlanWorkloadRequest):
    preparation_digest: str = Field(pattern=DIGEST_PATTERN)


class PlanVerifyRequest(PlanWorkloadRequest):
    expected_digest: str = Field(pattern=DIGEST_PATTERN)


class PlanWorkloadRequests(StrictModel):
    prepare: PlanPrepareRequest
    start: PlanStartRequest
    stop: PlanWorkloadRequest
    health: PlanWorkloadRequest
    verify: PlanVerifyRequest


class PlanEndpoint(StrictModel):
    scheme: Literal["http", "https"]
    port: int = Field(ge=1, le=65535, strict=True)
    path: str = Field(min_length=1, max_length=512, pattern=r"^/")


class PlanRelease(StrictModel):
    manifest_path: str = Field(min_length=1, max_length=512)
    manifest_sha256: str = Field(pattern=DIGEST_PATTERN)
    definition_hash: str = Field(pattern=DIGEST_PATTERN)
    release_request: PlanReleaseRequest
    workload_requests: PlanWorkloadRequests
    endpoint: PlanEndpoint


class PlanReleases(RootModel[dict[str, PlanRelease]]):
    root: dict[str, PlanRelease] = Field(max_length=128)


class PlanInputDigests(RootModel[dict[str, str]]):
    root: dict[str, str] = Field(max_length=512)


class PlanOperation(StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(pattern=NODE_PATTERN)
    workload_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: str = Field(min_length=1, max_length=80)
    dependencies: list[str] = Field(max_length=512)
    compensation_kind: str | None = Field(default=None, max_length=80)
    payload_digest: str = Field(pattern=DIGEST_PATTERN)


class PlanOperationGraph(StrictModel):
    schema_version: Literal[1]
    base_commit: str = Field(pattern=COMMIT_PATTERN)
    targets: list[str] = Field(max_length=512)
    nodes: list[PlanOperation] = Field(max_length=4096)


class ReconciliationPlanResponse(StrictModel):
    commit: str = Field(pattern=COMMIT_PATTERN)
    digest: str = Field(pattern=DIGEST_PATTERN)
    targets: list[str]
    placements: PlanPlacements
    routes: PlanRoutes
    releases: PlanReleases
    input_digests: PlanInputDigests
    reconciliation_id: str
    operation_graph: PlanOperationGraph
    agent_protocol_range: list[int] = Field(min_length=2, max_length=2)


class ReconciliationAcceptedResponse(StrictModel):
    base_commit: str = Field(pattern=COMMIT_PATTERN)
    job_id: str
    reconciliation_id: str | None = None
    state: str


class EndpointResponse(StrictModel):
    alias: str = Field(pattern=IDENTIFIER_PATTERN)
    api_base: str
    expires_at: str
    generation: int = Field(ge=1)
    node_id: str = Field(pattern=NODE_PATTERN)
    observed_at: str
    plan_digest: str = Field(pattern=DIGEST_PATTERN)
    state: str = Field(pattern=r"^published$")


class AgentSummary(StrictModel):
    node_id: str = Field(pattern=NODE_PATTERN)
    state: str
    protocol_version: int | None = Field(default=None, ge=1)
    capabilities: list[str]
    last_seen_at: str | None
    last_seen_age_seconds: float | None = Field(default=None, ge=0)
    stale: bool
    certificate_expires_at: str | None


class AgentsResponse(StrictModel):
    agents: list[AgentSummary]


class JobOperationProgress(StrictModel):
    phase: str = Field(min_length=1, max_length=80)


class JobOperationResponse(StrictModel):
    id: str
    graph_operation_id: str | None = None
    node_id: str = Field(pattern=NODE_PATTERN)
    kind: str
    state: str
    attempt: int = Field(ge=0)
    progress: JobOperationProgress | None = None
    updated_at: str | None = None


class JobProgress(StrictModel):
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    running: int = Field(ge=0)
    total: int = Field(ge=0)


class JobDetailResponse(StrictModel):
    id: str
    state: str
    kind: str
    base_commit: str
    targets: list[str]
    current_attempt: int = Field(ge=0)
    status_reason: str | None
    reconciliation_id: str | None
    operations: list[JobOperationResponse]
    progress: JobProgress


class JobResumeResponse(StrictModel):
    id: str
    state: str = Field(pattern=r"^queued$")


class JobSummary(StrictModel):
    id: str
    state: str
    kind: str


class JobsResponse(StrictModel):
    jobs: list[JobSummary]


class JobLogsResponse(StrictModel):
    job_id: str
    digests: list[str]


@dataclass(frozen=True)
class OperationApiServices:
    """Optional projections backed by accepted durable control state only."""

    endpoint: Callable[[str], Mapping[str, object]]
    agents: Callable[[], Sequence[Mapping[str, object]]]
    job_operations: Callable[[str], Sequence[Mapping[str, object]]]
    resume_job: Callable[[str], None]


def job_response(
    job: Any, operations: Sequence[Mapping[str, object]]
) -> JobDetailResponse:
    projected = [
        JobOperationResponse(
            id=str(item["id"]),
            graph_operation_id=(
                None
                if item.get("graph_operation_id") is None
                else str(item["graph_operation_id"])
            ),
            node_id=str(item["node_id"]),
            kind=str(item["kind"]),
            state=str(item["state"]),
            attempt=int(item["attempt"]),
            progress=_progress_projection(item.get("progress")),
            updated_at=(
                None
                if item.get("updated_at") is None
                else str(item["updated_at"])
            ),
        )
        for item in operations
    ]
    states = [item.state for item in projected]
    terminal = {"succeeded", "accepted", "compensated"}
    return JobDetailResponse(
        id=str(job.id),
        state=str(job.state),
        kind=str(job.kind),
        base_commit=str(job.base_commit),
        targets=list(job.targets),
        current_attempt=int(job.current_attempt),
        status_reason=job.status_reason,
        reconciliation_id=job.reconciliation_id,
        operations=projected,
        progress=JobProgress(
            completed=sum(state in terminal for state in states),
            failed=sum(state in {"failed", "uncertain"} for state in states),
            running=sum(
                state in {"queued", "running", "planned", "compensating"}
                for state in states
            ),
            total=len(states),
        ),
    )


def _progress_projection(value: object) -> JobOperationProgress | None:
    if not isinstance(value, Mapping):
        return None
    phase = value.get("phase")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 80:
        return None
    return JobOperationProgress(phase=phase)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _DurableOperationProjection:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        route_root: Path,
        *,
        clock: Callable[[], datetime],
        stale_after_seconds: int,
    ) -> None:
        if route_root.is_symlink() or stale_after_seconds <= 0:
            raise ValueError("operation projection configuration is invalid")
        self._sessions = sessions
        self._route_root = route_root
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds

    def endpoint(self, alias: str) -> Mapping[str, object]:
        with self._sessions() as session:
            owner = session.get(RoutePublicationOwner, 1)
            publication = (
                None
                if owner is None or owner.reconciliation_id is None
                else session.get(RoutePublication, owner.reconciliation_id)
            )
            reconciliation = (
                None
                if owner is None or owner.reconciliation_id is None
                else session.get(Reconciliation, owner.reconciliation_id)
            )
            if (
                owner is None
                or publication is None
                or reconciliation is None
                or publication.state not in _ACTIVE_PUBLICATION_STATES
                or reconciliation.status != "succeeded"
                or reconciliation.current_phase != "completed"
                or publication.generation != owner.owner_generation
                or publication.activation_marker is None
                or publication.activation_marker_digest is None
                or publication.route_digest is None
                or publication.lease_expires_at is None
                or _aware(publication.lease_expires_at) <= _aware(self._clock())
            ):
                raise RuntimeError("active publication is unavailable")
            marker = dict(publication.activation_marker)
            marker_digest = publication.activation_marker_digest
            route_digest = publication.route_digest
            evidence_digest = publication.evidence_digest
            litellm_digest = publication.litellm_digest
            bundle_digest = publication.bundle_digest
            lease_issued_at = publication.lease_issued_at
            lease_expires_at = publication.lease_expires_at
            owner_reconciliation_id = owner.reconciliation_id
            owner_generation = owner.owner_generation
            publication_generation = publication.generation
            publication_plan_digest = publication.plan_digest

        bundle = verify_active_route_bundle(
            self._route_root,
            clock=self._clock,
        )
        active_marker = bundle.marker
        if (
            asdict(active_marker) != marker
            or active_marker.digest != marker_digest
            or active_marker.state != "published"
            or active_marker.reconciliation_id != owner_reconciliation_id
            or active_marker.plan_digest != publication_plan_digest
            or active_marker.generation != publication_generation
            or active_marker.generation != owner_generation
            or active_marker.evidence_set_digest != evidence_digest
            or active_marker.routes_sha256 != route_digest
            or active_marker.litellm_sha256 != litellm_digest
            or active_marker.manifest_sha256 != bundle_digest
            or lease_issued_at is None
            or lease_expires_at is None
            or _aware(lease_issued_at)
            != _aware(datetime.fromisoformat(active_marker.issued_at))
            or _aware(lease_expires_at)
            != _aware(datetime.fromisoformat(active_marker.expires_at))
        ):
            raise RuntimeError("activation marker does not match durable state")
        routes = bundle.routes
        if (
            routes.get("generation") != publication_generation
            or routes.get("state") != "published"
            or not isinstance(routes.get("routes"), Mapping)
        ):
            raise RuntimeError("active route state does not match publication")
        raw = routes["routes"].get(alias)
        if raw is None:
            raise KeyError(alias)
        if not isinstance(raw, Mapping):
            raise OperationProjectionError("active endpoint is invalid")
        scheme = raw.get("scheme")
        address = raw.get("address")
        port = raw.get("port")
        path = raw.get("path")
        node_id = raw.get("node_id")
        observed_at = raw.get("observed_at")
        if (
            scheme not in {"http", "https"}
            or not isinstance(address, str)
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
            or not isinstance(path, str)
            or not path.startswith("/")
            or not isinstance(node_id, str)
            or re.fullmatch(NODE_PATTERN, node_id) is None
            or not isinstance(observed_at, str)
        ):
            raise RuntimeError("active endpoint is invalid")
        return {
            "alias": alias,
            "api_base": f"{scheme}://{address}:{port}{path.rstrip('/')}",
            "expires_at": active_marker.expires_at,
            "generation": active_marker.generation,
            "node_id": node_id,
            "observed_at": observed_at,
            "plan_digest": active_marker.plan_digest,
            "state": "published",
        }

    def agents(self) -> Sequence[Mapping[str, object]]:
        now = _aware(self._clock())
        with self._sessions() as session:
            nodes = list(
                session.scalars(select(AgentNode).order_by(AgentNode.node_id).limit(500))
            )
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(
                        AgentCertificate.state == "active",
                        AgentCertificate.revoked_at.is_(None),
                    )
                    .order_by(
                        AgentCertificate.node_id,
                        AgentCertificate.not_after.desc(),
                        AgentCertificate.generation.desc(),
                    )
                )
            )
        latest_certificates: dict[str, AgentCertificate] = {}
        for certificate in certificates:
            latest_certificates.setdefault(certificate.node_id, certificate)
        projected: list[Mapping[str, object]] = []
        for node in nodes:
            last_seen = None if node.last_seen_at is None else _aware(node.last_seen_at)
            age = None if last_seen is None else max(0.0, (now - last_seen).total_seconds())
            certificate = latest_certificates.get(node.node_id)
            not_after = (
                None if certificate is None else _aware(certificate.not_after)
            )
            projected.append(
                {
                    "capabilities": [
                        capability[:80]
                        for capability in node.capabilities[:64]
                        if isinstance(capability, str)
                    ],
                    "certificate_expires_at": (
                        None if not_after is None else not_after.isoformat()
                    ),
                    "last_seen_age_seconds": age,
                    "last_seen_at": None if last_seen is None else last_seen.isoformat(),
                    "node_id": node.node_id,
                    "protocol_version": node.protocol_version,
                    "stale": age is None or age > self._stale_after_seconds,
                    "state": node.state,
                }
            )
        return projected

    def job_operations(self, job_id: str) -> Sequence[Mapping[str, object]]:
        with self._sessions() as session:
            operations = list(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == job_id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                    .limit(1000)
                )
            )
            graph_ids = {
                row.agent_operation_id: row.graph_operation_id
                for row in session.scalars(
                    select(ReconciliationOperation).where(
                        ReconciliationOperation.agent_operation_id.in_(
                            [operation.id for operation in operations]
                        )
                    )
                )
                if row.agent_operation_id is not None
            }
            attempts = {
                attempt.operation_id: attempt
                for attempt in session.scalars(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id.in_(
                            [operation.id for operation in operations]
                        )
                    )
                )
                if any(
                    operation.id == attempt.operation_id
                    and operation.current_attempt == attempt.attempt
                    for operation in operations
                )
            }
        return [
            {
                "attempt": operation.current_attempt,
                "graph_operation_id": graph_ids.get(operation.id),
                "id": operation.id,
                "kind": operation.kind,
                "node_id": operation.node_id,
                "progress": (
                    None
                    if attempts.get(operation.id) is None
                    else (
                        None
                        if _progress_projection(
                            attempts[operation.id].progress
                        )
                        is None
                        else _progress_projection(
                            attempts[operation.id].progress
                        ).model_dump(mode="json")
                    )
                ),
                "state": operation.state,
                "updated_at": _aware(operation.updated_at).isoformat(),
            }
            for operation in operations
        ]

    def resume_job(self, job_id: str) -> None:
        with self._sessions.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state != "waiting-for-operator":
                raise ValueError("job is not waiting for operator")
            job.state = "queued"
            job.status_reason = None
            job.updated_at = self._clock()


def durable_operation_services(
    sessions: sessionmaker[Session],
    route_root: Path,
    *,
    clock: Callable[[], datetime],
    stale_after_seconds: int = 150,
) -> OperationApiServices:
    """Build bounded projections over database state and the active route bundle."""

    projection = _DurableOperationProjection(
        sessions,
        route_root,
        clock=clock,
        stale_after_seconds=stale_after_seconds,
    )
    return OperationApiServices(
        endpoint=projection.endpoint,
        agents=projection.agents,
        job_operations=projection.job_operations,
        resume_job=projection.resume_job,
    )


def admin_openapi_schema(app: Any) -> dict[str, object]:
    """Return the deterministic authenticated admin surface without agent APIs."""

    source = deepcopy(app.openapi())
    paths: dict[str, object] = {}
    for path, path_item in source.get("paths", {}).items():
        if path in {"/api/v1/healthz", "/api/v1/readyz"}:
            continue
        if not path.startswith("/api/v1/"):
            continue
        selected = deepcopy(path_item)
        for method, operation in selected.items():
            if method not in _HTTP_METHODS:
                continue
            try:
                operation["operationId"] = _ADMIN_OPERATION_IDS[(method, path)]
            except KeyError as error:
                raise RuntimeError(
                    f"admin operation ID is not explicit for {method.upper()} {path}"
                ) from error
            operation["security"] = [{"BearerAuth": []}]
        paths[path] = selected
    source["paths"] = paths
    components = source.setdefault("components", {})
    components["securitySchemes"] = {
        "BearerAuth": {"scheme": "bearer", "type": "http"}
    }

    referenced: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith(
                "#/components/schemas/"
            ):
                referenced.add(reference.rsplit("/", 1)[-1])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(paths)
    schemas = components.get("schemas", {})
    pending = list(referenced)
    while pending:
        name = pending.pop()
        before = set(referenced)
        collect(schemas.get(name, {}))
        pending.extend(sorted(referenced - before))
    components["schemas"] = {
        name: schemas[name] for name in sorted(referenced) if name in schemas
    }
    return source


class NodeStatus(StrictModel):
    id: str = Field(pattern=NODE_PATTERN)
    display_name: str
    hostname: str
    lifecycle: str
    healthy: bool | None
    stale: bool
    labels: dict[str, str]
    profile: str | None
    memory_available_bytes: int = Field(ge=0)
    disk_available_bytes: int = Field(ge=0)
    probe_age_seconds: float | None = Field(default=None, ge=0)
    agent_state: str = "unregistered"
    last_seen_at: str | None = None
    last_seen_age_seconds: float | None = Field(default=None, ge=0)
    agent_last_seen_at: str | None = None
    agent_online: bool = False
    certificate_expires_at: str | None = None
    certificate_expiry_seconds: float | None = Field(default=None, ge=0)
    compatibility: str = "unknown"


class FleetStatusResponse(StrictModel):
    commit: str = Field(pattern=COMMIT_PATTERN)
    nodes: list[NodeStatus]


def plan_response(plan: Any) -> ReconciliationPlanResponse:
    """Project an accepted planner result without changing its canonical fields."""

    routes = {
        alias: _plan_route(value)
        for alias, value in _plan_mapping(plan.routes, "plan routes").items()
    }
    releases = {
        workload_id: _plan_release(value)
        for workload_id, value in _plan_mapping(
            plan.releases, "plan releases"
        ).items()
    }
    graph = _plan_mapping(plan.operation_graph.document, "operation graph")
    raw_nodes = graph.get("nodes")
    if not isinstance(raw_nodes, (list, tuple)):
        raise TypeError("operation graph nodes are invalid")
    return ReconciliationPlanResponse(
        commit=plan.commit,
        digest=plan.digest,
        targets=list(plan.targets),
        placements=PlanPlacements(root=dict(plan.placements)),
        routes=PlanRoutes(root=routes),
        releases=PlanReleases(root=releases),
        input_digests=PlanInputDigests(root=dict(plan.input_digests)),
        reconciliation_id=plan.operation_graph.reconciliation_id,
        operation_graph=PlanOperationGraph(
            schema_version=graph["schema_version"],
            base_commit=graph["base_commit"],
            targets=graph["targets"],
            nodes=[_plan_operation(value) for value in raw_nodes],
        ),
        agent_protocol_range=list(plan.agent_protocol_range),
    )


def _plan_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise TypeError(f"{field} is invalid")
    return value


def _plan_route(value: object) -> PlanRoute:
    raw = _plan_mapping(value, "plan route")
    quota = _plan_mapping(raw["quota"], "plan route quota")
    return PlanRoute(
        workload_id=raw["workload_id"],
        nodes=raw["nodes"],
        entrypoint_node_id=raw["entrypoint_node_id"],
        scheme=raw["scheme"],
        port=raw["port"],
        path=raw["path"],
        quota=PlanQuota(
            requests_per_minute=quota["requests_per_minute"],
            tokens_per_minute=quota["tokens_per_minute"],
        ),
        quota_digest=raw["quota_digest"],
    )


def _plan_release(value: object) -> PlanRelease:
    raw = _plan_mapping(value, "plan release")
    request = _plan_mapping(raw["release_request"], "release request")
    requests = _plan_mapping(raw["workload_requests"], "workload requests")
    prepare = _plan_mapping(requests["prepare"], "prepare request")
    start = _plan_mapping(requests["start"], "start request")
    stop = _plan_mapping(requests["stop"], "stop request")
    health = _plan_mapping(requests["health"], "health request")
    verify = _plan_mapping(requests["verify"], "verify request")
    endpoint = _plan_mapping(raw["endpoint"], "release endpoint")
    return PlanRelease(
        manifest_path=raw["manifest_path"],
        manifest_sha256=raw["manifest_sha256"],
        definition_hash=raw["definition_hash"],
        release_request=PlanReleaseRequest(
            schema_version=request["schema_version"],
            target_name=request["target_name"],
            oci_manifest_digest=request["oci_manifest_digest"],
            target_digest=request["target_digest"],
            provenance_digest=request["provenance_digest"],
            adapter_id=request["adapter_id"],
        ),
        workload_requests=PlanWorkloadRequests(
            prepare=PlanPrepareRequest(
                **_base_workload_request(prepare),
                profile_digest=prepare["profile_digest"],
            ),
            start=PlanStartRequest(
                **_base_workload_request(start),
                preparation_digest=start["preparation_digest"],
            ),
            stop=PlanWorkloadRequest(**_base_workload_request(stop)),
            health=PlanWorkloadRequest(**_base_workload_request(health)),
            verify=PlanVerifyRequest(
                **_base_workload_request(verify),
                expected_digest=verify["expected_digest"],
            ),
        ),
        endpoint=PlanEndpoint(
            scheme=endpoint["scheme"],
            port=endpoint["port"],
            path=endpoint["path"],
        ),
    )


def _base_workload_request(
    raw: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": raw["schema_version"],
        "workload_id": raw["workload_id"],
        "release_digest": raw["release_digest"],
        "adapter_id": raw["adapter_id"],
    }


def _plan_operation(value: object) -> PlanOperation:
    raw = _plan_mapping(value, "plan operation")
    return PlanOperation(
        operation_id=raw["operation_id"],
        node_id=raw["node_id"],
        workload_id=raw["workload_id"],
        kind=raw["kind"],
        dependencies=raw["dependencies"],
        compensation_kind=raw["compensation_kind"],
        payload_digest=raw["payload_digest"],
    )
