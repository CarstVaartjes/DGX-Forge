"""Versioned authenticated control API."""

from __future__ import annotations

import base64
import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dgx_agent_protocol import canonical_message
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse

from .agent_api import (
    AgentApiServices,
    EnrollmentRateLimiter,
    activation_agent_identity,
    active_agent_identity,
    install_agent_routes,
)
from .audit import AuditRecord
from .auth import (
    MUTATION_ROLES,
    Actor,
    AuthError,
    TokenCodec,
    TrustedProxyAgentIdentityMiddleware,
)
from .metrics import MetricsRegistry
from .proposals import DocumentChange


@dataclass(frozen=True)
class AdminServices:
    repository: Any
    proposals: Any
    changes: Any | None
    reconciler: Any | None


def build_agent_services(settings: Any, sessions: Any, clock: Callable[[], Any]) -> AgentApiServices:
    """Construct the fail-closed production agent runtime from one provider."""
    from .agent_jobs import AgentJobService
    from .enrollment import EnrollmentService
    from .pki import BuiltinCertificateAuthority
    from .step_ca import StepCertificateAuthority

    if settings.agent_runtime != "enabled":
        raise RuntimeError("agent runtime is disabled")
    if settings.agent_intermediate_certificate_path is None:
        raise RuntimeError("agent intermediate certificate path is unavailable")
    if settings.agent_ca_provider == "step-ca":
        if settings.agent_ca_root_path is None or settings.agent_ca_credential_path is None or settings.agent_ca_provisioner_public_jwk_path is None:
            raise RuntimeError("step-ca provider files are unavailable")
        authority = StepCertificateAuthority(
            ca_url=settings.agent_ca_url,
            root_certificate_path=settings.agent_ca_root_path,
            intermediate_certificate_path=settings.agent_intermediate_certificate_path,
            provisioner_name=settings.agent_ca_provisioner_name,
            provisioner_kid=settings.agent_ca_provisioner_kid,
            credential_path=settings.agent_ca_credential_path,
            provisioner_public_jwk_path=settings.agent_ca_provisioner_public_jwk_path,
            timeout_seconds=settings.agent_ca_timeout_seconds,
            max_response_bytes=settings.agent_ca_max_response_bytes,
        )
        authority.check_health()
    elif settings.agent_ca_provider == "builtin":
        if settings.agent_intermediate_key_path is None:
            raise RuntimeError("built-in intermediate key path is unavailable")
        authority = BuiltinCertificateAuthority(
            settings.agent_intermediate_key_path,
            settings.agent_intermediate_certificate_path,
        )
    else:
        raise RuntimeError("agent CA provider is unavailable")
    settings.agent_artifact_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    return AgentApiServices(
        enrollment=EnrollmentService(sessions, authority, clock=clock),
        operations=AgentJobService(sessions, clock=clock),
        sessions=sessions,
        clock=clock,
        artifact_root=settings.agent_artifact_root,
    )


class SpaFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code == 404 and "." not in path:
                return FileResponse(Path(self.directory) / "index.html")
            raise


class JobQueue(Protocol):
    def enqueue(self, kind: str, actor: str, base_commit: str, targets: Sequence[str], payload: Mapping[str, object], *, request_id: str) -> Any: ...
    def get(self, job_id: str) -> Any: ...
    def list(self, *, limit: int = 100) -> list[Any]: ...


class AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...
    def list(self, *, limit: int = 100) -> list[AuditRecord]: ...


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=80)
    base_commit: str = Field(min_length=1, max_length=128)
    targets: list[str] = Field(max_length=64)
    payload: dict[str, object]


class JobResponse(BaseModel):
    id: str
    state: str


class ProposalChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    document: dict[str, object]


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    changes: list[ProposalChangeRequest] = Field(min_length=1, max_length=32)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconciliationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def create_app(
    *,
    jobs: JobQueue,
    tokens: TokenCodec,
    audits: AuditSink,
    fleet: Callable[[], Mapping[str, object]],
    now: Callable[[], int] = lambda: int(time.time()),
    admin: AdminServices | None = None,
    metrics: MetricsRegistry | None = None,
    metrics_token: str | None = None,
    metrics_refresh: Callable[[], None] | None = None,
    job_logs=None,
    agent: AgentApiServices | None = None,
    trusted_agent_proxy_auth: bytes = b"",
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
) -> FastAPI:
    app = FastAPI(title="DGX Forge Control", version="1.0", docs_url=None, redoc_url=None)

    @app.exception_handler(StarletteHTTPException)
    async def canonical_agent_http_error(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        if not request.url.path.startswith("/agent/v1/"):
            return await http_exception_handler(request, error)
        return Response(
            content=canonical_message({"detail": jsonable_encoder(error.detail)}),
            status_code=error.status_code,
            headers=error.headers,
            media_type="application/json",
        )

    @app.exception_handler(RequestValidationError)
    async def canonical_agent_validation_error(
        request: Request, error: RequestValidationError
    ) -> Response:
        if not request.url.path.startswith("/agent/v1/"):
            return await request_validation_exception_handler(request, error)
        return Response(
            content=canonical_message({"detail": jsonable_encoder(error.errors())}),
            status_code=422,
            media_type="application/json",
        )

    app.add_middleware(
        TrustedProxyAgentIdentityMiddleware,
        trusted_proxy_auth=trusted_agent_proxy_auth,
        agent_identity_validator=(lambda identity: active_agent_identity(agent, identity)) if agent is not None else None,
        activation_identity_validator=(
            lambda identity: activation_agent_identity(agent, identity)
        ) if agent is not None else None,
    )

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        started = time.monotonic()
        request_id = request.headers.get("x-request-id")
        try:
            request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
        except ValueError:
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        length = request.headers.get("content-length")
        if length and int(length) > 1_048_576 and request.url.path != "/agent/v1/enroll":
            response = Response(status_code=413)
        else:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-content-type-options"] = "nosniff"
        if metrics is not None:
            metrics.observe_api(request.method, response.status_code, time.monotonic() - started)
        return response

    def actor(request: Request) -> Actor:
        authorization = request.headers.get("authorization", "")
        cookie_auth = False
        if authorization.startswith("Bearer "):
            encoded = authorization.removeprefix("Bearer ")
        else:
            encoded = request.cookies.get("dgx_session", "")
            cookie_auth = bool(encoded)
        if not encoded:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            authenticated = tokens.verify(encoded, now=now())
        except AuthError:
            raise HTTPException(status_code=401, detail="authentication failed") from None
        if cookie_auth and request.method not in {"GET", "HEAD", "OPTIONS"}:
            cookie = request.cookies.get("dgx_csrf")
            header = request.headers.get("x-csrf-token")
            if not cookie or not header or not secrets.compare_digest(cookie, header):
                raise HTTPException(status_code=403, detail="CSRF validation failed")
        return authenticated

    def require_mutation_role(authenticated: Actor, path: str) -> None:
        if authenticated.role not in MUTATION_ROLES[("POST", path)]:
            raise HTTPException(status_code=403, detail="insufficient role")

    install_agent_routes(
        app,
        actor_dependency=actor,
        services=agent,
        enrollment_rate_limiter=enrollment_rate_limiter,
    )
    authenticated_actor = Depends(actor)

    @app.get("/api/v1/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/readyz")
    def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def platform_metrics(request: Request) -> Response:
        if metrics is None or metrics_token is None:
            raise HTTPException(status_code=404, detail="not found")
        authorization = request.headers.get("authorization", "")
        if not secrets.compare_digest(authorization, f"Bearer {metrics_token}"):
            raise HTTPException(status_code=401, detail="authentication required")
        if metrics_refresh is not None:
            metrics_refresh()
        return Response(metrics.render(), media_type="application/openmetrics-text; version=1.0.0; charset=utf-8")

    @app.get("/api/v1/fleet")
    def fleet_view(_actor: Actor = authenticated_actor) -> Mapping[str, object]:
        return fleet()

    @app.get("/api/v1/repository")
    def repository_view(commit: str | None = None, _actor: Actor = authenticated_actor) -> dict[str, object]:
        if admin is None:
            raise HTTPException(status_code=503, detail="repository administration unavailable")
        resolved = commit or admin.repository.head()
        snapshot = admin.repository.inspect(resolved)
        return {"commit": snapshot.commit, "documents": dict(snapshot.documents), "dependencies": dict(snapshot.dependencies)}

    @app.get("/api/v1/documents")
    def document_view(commit: str | None = None, path: str | None = None, kind: str | None = None, _actor: Actor = authenticated_actor) -> dict[str, object]:
        if admin is None:
            raise HTTPException(status_code=503, detail="repository administration unavailable")
        resolved = commit or admin.repository.head()
        if path is None:
            snapshot = admin.repository.inspect(resolved)
            prefixes = {
                "models": ("config/workloads/", "locks/", "manifests/"),
                "profiles": ("config/cluster-profiles/",),
            }
            selected = prefixes.get(kind or "", ())
            if not selected:
                raise HTTPException(status_code=400, detail="document kind is invalid")
            return {"commit": resolved, "documents": [name for name in snapshot.documents if name.startswith(selected)]}
        document = admin.repository.read_document(resolved, path)
        return {"commit": document.commit, "path": document.path, "sha256": document.sha256, "document": document.parsed}

    @app.post("/api/v1/proposals")
    def proposal_preview(body: ProposalRequest, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/proposals")
        if admin is None:
            raise HTTPException(status_code=503, detail="repository administration unavailable")
        preview = admin.proposals.preview(
            authenticated.subject,
            body.base_commit,
            [DocumentChange(change.path, change.document) for change in body.changes],
        )
        return {
            "base_commit": preview.base_commit,
            "digest": preview.digest,
            "patch": base64.b64encode(preview.patch).decode(),
            "affected_documents": list(preview.affected_documents),
            "validation_results": list(preview.validation_results),
        }

    @app.post("/api/v1/changes", status_code=status.HTTP_202_ACCEPTED)
    def submit_change(body: ChangeRequest, request: Request, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/changes")
        if admin is None or admin.changes is None:
            raise HTTPException(status_code=503, detail="change submission unavailable")
        result = admin.changes.submit(body.proposal_digest, authenticated.subject, request.state.request_id)
        audits.append(AuditRecord(request.state.request_id, authenticated.subject, "repository.change.submit", None, ()))
        return dict(result)

    @app.post("/api/v1/reconciliations/plan")
    def reconcile_plan(body: ReconciliationPlanRequest, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/reconciliations/plan")
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        plan = admin.reconciler.plan(body.commit, body.profile_id)
        return {
            "commit": plan.commit, "digest": plan.digest, "targets": list(plan.targets),
            "placements": dict(plan.placements), "routes": dict(plan.routes),
            "releases": dict(plan.releases), "input_digests": dict(plan.input_digests),
            "operation_graph": plan.operation_graph.document,
            "agent_protocol_range": list(plan.agent_protocol_range),
        }

    @app.post("/api/v1/reconciliations", status_code=status.HTTP_202_ACCEPTED)
    def reconcile(body: ReconciliationRequest, request: Request, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        require_mutation_role(authenticated, "/api/v1/reconciliations")
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        return dict(admin.reconciler.enqueue(body.plan_digest, authenticated.subject, request.state.request_id))

    @app.post("/api/v1/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
    def enqueue(body: JobRequest, request: Request, authenticated: Actor = authenticated_actor) -> JobResponse:
        require_mutation_role(authenticated, "/api/v1/jobs")
        job = jobs.enqueue(body.kind, authenticated.subject, body.base_commit, body.targets, body.payload, request_id=request.state.request_id)
        audits.append(AuditRecord(request.state.request_id, authenticated.subject, f"job.enqueue:{body.kind}", body.base_commit, tuple(body.targets)))
        return JobResponse(id=str(job.id), state=str(job.state))

    @app.get("/api/v1/jobs")
    def jobs_view(_actor: Actor = authenticated_actor) -> dict[str, object]:
        return {"jobs": [{"id": str(job.id), "state": str(job.state), "kind": str(job.kind)} for job in jobs.list()]}

    @app.get("/api/v1/audit")
    def audit_view(_actor: Actor = authenticated_actor) -> dict[str, object]:
        return {"events": [
            {"request_id": event.request_id, "actor": event.actor, "action": event.action, "base_commit": event.base_commit, "targets": list(event.targets)}
            for event in audits.list()
        ]}

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    def job_view(job_id: str, _actor: Actor = authenticated_actor) -> JobResponse:
        try:
            job = jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        return JobResponse(id=str(job.id), state=str(job.state))

    @app.get("/api/v1/jobs/{job_id}/logs")
    def job_log_list(job_id: str, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if job_logs is None:
            raise HTTPException(status_code=503, detail="job logs unavailable")
        try:
            jobs.get(job_id)
            return {"job_id": job_id, "digests": list(job_logs.list(job_id))}
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="job not found") from None

    @app.get("/api/v1/jobs/{job_id}/logs/{digest}")
    def job_log_content(job_id: str, digest: str, authenticated: Actor = authenticated_actor) -> Response:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if job_logs is None:
            raise HTTPException(status_code=503, detail="job logs unavailable")
        try:
            jobs.get(job_id)
            return Response(job_logs.read(job_id, digest), media_type="text/plain; charset=utf-8")
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="job log not found") from None

    return app


def production_app() -> FastAPI:
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from .audit import SqlAuditStore
    from .code_host import RepositoryCodeHost
    from .dashboard import DashboardService
    from .db import build_engine, session_factory
    from .desired_state import (
        DesiredStateResolver,
        durable_desired_state_observations,
    )
    from .git_policy import GitPolicy, PolicyStore
    from .jobs import JobService
    from .logging import JobLogStore
    from .metrics import MetricsRegistry, OperationalMetricsCollector
    from .models import Job
    from .offline import OnlineLock
    from .proposals import ProposalService
    from .reconcile import ChangeService, Reconciler
    from .repository import RepositoryService
    from .settings import Settings

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    online_lock = OnlineLock(settings.state_path / "offline.lock")
    online_lock.__enter__()
    job_service = JobService(sessions, clock=clock)
    repository = RepositoryService(settings.repository_path)
    proposals = ProposalService(repository, head=repository.head)
    if settings.git_signing_key_path is None:
        raise RuntimeError("production Git signing key is unavailable")
    policy_store = PolicyStore(settings.state_path / "git-policy")
    code_host = RepositoryCodeHost(
        settings.repository_path,
        signing_key=settings.git_signing_key_path,
        lock_path=settings.state_path / "git-change.lock",
    )
    git_policy = GitPolicy(
        policy_store, code_host,
        protected_branch=settings.deployment_branch,
        required_checks=settings.required_checks,
    )
    changes = ChangeService(proposals, git_policy)
    reconciler = Reconciler(
        git_policy,
        DesiredStateResolver(repository, clock=clock),
        jobs=job_service,
        observations=lambda: durable_desired_state_observations(sessions),
    )
    dashboard = DashboardService(repository, sessions)
    metrics = MetricsRegistry()
    operational_metrics = OperationalMetricsCollector(metrics, sessions, clock=clock)
    agent_services = build_agent_services(settings, sessions, clock)
    def refresh_metrics() -> None:
        operational_metrics.refresh()
        fleet_state = dashboard.fleet()
        for node in fleet_state["nodes"]:
            metrics.update_node(
                node["id"], ready=node["healthy"] is True,
                memory_available_bytes=int(node["memory_available_bytes"]),
                disk_available_bytes=int(node["disk_available_bytes"]),
                probe_age_seconds=float(node["probe_age_seconds"]),
            )
        with sessions() as session:
            for kind, state, count in session.execute(select(Job.kind, Job.state, func.count()).group_by(Job.kind, Job.state)):
                metrics.set_job_count(kind, state, count)
        backup_marker = settings.state_path / "last-successful-backup.epoch"
        if backup_marker.is_file() and not backup_marker.is_symlink():
            try:
                completed_at = int(backup_marker.read_text().strip())
                metrics.set_backup_age(max(0, int(time.time()) - completed_at))
            except (OSError, ValueError):
                pass
    app = create_app(
        jobs=job_service,
        tokens=TokenCodec(settings.token_signing_key),
        audits=SqlAuditStore(sessions, clock),
        fleet=dashboard.fleet,
        admin=AdminServices(repository, proposals, changes, reconciler),
        metrics=metrics,
        metrics_token=settings.metrics_token,
        metrics_refresh=refresh_metrics,
        job_logs=JobLogStore(settings.state_path / "job-logs"),
        agent=agent_services,
        trusted_agent_proxy_auth=settings.agent_proxy_auth,
    )
    web_root = Path(__file__).resolve().parent / "web"
    if web_root.is_dir():
        app.mount("/", SpaFiles(directory=web_root, html=True), name="admin-web")
    @app.on_event("shutdown")
    def release_online_lock() -> None:
        online_lock.__exit__()
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
