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

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditRecord
from .auth import Actor, AuthError, TokenCodec
from .proposals import DocumentChange
from .metrics import MetricsRegistry


@dataclass(frozen=True)
class AdminServices:
    repository: Any
    proposals: Any
    changes: Any | None
    reconciler: Any | None


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
) -> FastAPI:
    app = FastAPI(title="DGX Forge Control", version="1.0", docs_url=None, redoc_url=None)

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
        if length and int(length) > 1_048_576:
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
    def fleet_view(_actor: Actor = Depends(actor)) -> Mapping[str, object]:
        return fleet()

    @app.get("/api/v1/repository")
    def repository_view(commit: str | None = None, _actor: Actor = Depends(actor)) -> dict[str, object]:
        if admin is None:
            raise HTTPException(status_code=503, detail="repository administration unavailable")
        resolved = commit or admin.repository.head()
        snapshot = admin.repository.inspect(resolved)
        return {"commit": snapshot.commit, "documents": dict(snapshot.documents), "dependencies": dict(snapshot.dependencies)}

    @app.get("/api/v1/documents")
    def document_view(commit: str | None = None, path: str | None = None, kind: str | None = None, _actor: Actor = Depends(actor)) -> dict[str, object]:
        if admin is None:
            raise HTTPException(status_code=503, detail="repository administration unavailable")
        resolved = commit or admin.repository.head()
        if path is None:
            snapshot = admin.repository.inspect(resolved)
            prefixes = {
                "models": ("config/workloads/", "locks/", "manifests/"),
                "profiles": ("config/cluster-profiles/",),
            }
            selected = prefixes.get(kind or "", tuple())
            if not selected:
                raise HTTPException(status_code=400, detail="document kind is invalid")
            return {"commit": resolved, "documents": [name for name in snapshot.documents if name.startswith(selected)]}
        document = admin.repository.read_document(resolved, path)
        return {"commit": document.commit, "path": document.path, "sha256": document.sha256, "document": document.parsed}

    @app.post("/api/v1/proposals")
    def proposal_preview(body: ProposalRequest, authenticated: Actor = Depends(actor)) -> dict[str, object]:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
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
    def submit_change(body: ChangeRequest, request: Request, authenticated: Actor = Depends(actor)) -> dict[str, object]:
        if authenticated.role != "administrator":
            raise HTTPException(status_code=403, detail="administrator role required")
        if admin is None or admin.changes is None:
            raise HTTPException(status_code=503, detail="change submission unavailable")
        result = admin.changes.submit(body.proposal_digest, authenticated.subject, request.state.request_id)
        audits.append(AuditRecord(request.state.request_id, authenticated.subject, "repository.change.submit", None, ()))
        return dict(result)

    @app.post("/api/v1/reconciliations/plan")
    def reconcile_plan(body: ReconciliationPlanRequest, authenticated: Actor = Depends(actor)) -> dict[str, object]:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        plan = admin.reconciler.plan(body.commit)
        return {
            "commit": plan.commit, "digest": plan.digest, "targets": list(plan.targets),
            "placements": dict(plan.placements), "routes": dict(plan.routes),
            "releases": dict(plan.releases), "input_digests": dict(plan.input_digests),
        }

    @app.post("/api/v1/reconciliations", status_code=status.HTTP_202_ACCEPTED)
    def reconcile(body: ReconciliationRequest, request: Request, authenticated: Actor = Depends(actor)) -> dict[str, object]:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        return dict(admin.reconciler.enqueue(body.plan_digest, authenticated.subject, request.state.request_id))

    @app.post("/api/v1/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
    def enqueue(body: JobRequest, request: Request, authenticated: Actor = Depends(actor)) -> JobResponse:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        job = jobs.enqueue(body.kind, authenticated.subject, body.base_commit, body.targets, body.payload, request_id=request.state.request_id)
        audits.append(AuditRecord(request.state.request_id, authenticated.subject, f"job.enqueue:{body.kind}", body.base_commit, tuple(body.targets)))
        return JobResponse(id=str(job.id), state=str(job.state))

    @app.get("/api/v1/jobs")
    def jobs_view(_actor: Actor = Depends(actor)) -> dict[str, object]:
        return {"jobs": [{"id": str(job.id), "state": str(job.state), "kind": str(job.kind)} for job in jobs.list()]}

    @app.get("/api/v1/audit")
    def audit_view(_actor: Actor = Depends(actor)) -> dict[str, object]:
        return {"events": [
            {"request_id": event.request_id, "actor": event.actor, "action": event.action, "base_commit": event.base_commit, "targets": list(event.targets)}
            for event in audits.list()
        ]}

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    def job_view(job_id: str, _actor: Actor = Depends(actor)) -> JobResponse:
        try:
            job = jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found") from None
        return JobResponse(id=str(job.id), state=str(job.state))

    return app


def production_app() -> FastAPI:
    from datetime import UTC, datetime

    from .audit import SqlAuditStore
    from .db import build_engine, session_factory
    from .jobs import JobService
    from .offline import OnlineLock
    from .settings import Settings
    from .repository import RepositoryService
    from .proposals import ProposalService
    from .dashboard import DashboardService
    from .metrics import MetricsRegistry
    from .models import Job
    from sqlalchemy import func, select

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    online_lock = OnlineLock(settings.state_path / "offline.lock")
    online_lock.__enter__()
    job_service = JobService(sessions, clock=clock)
    repository = RepositoryService(settings.repository_path)
    proposals = ProposalService(repository, head=repository.head)
    dashboard = DashboardService(repository, sessions)
    metrics = MetricsRegistry()
    def refresh_metrics() -> None:
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
        admin=AdminServices(repository, proposals, None, None),
        metrics=metrics,
        metrics_token=settings.metrics_token,
        metrics_refresh=refresh_metrics,
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
