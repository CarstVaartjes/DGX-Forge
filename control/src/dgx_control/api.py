"""Versioned authenticated control API."""

from __future__ import annotations

import base64
import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditRecord
from .auth import Actor, AuthError, TokenCodec
from .proposals import DocumentChange


@dataclass(frozen=True)
class AdminServices:
    repository: Any
    proposals: Any
    changes: Any | None
    reconciler: Any | None


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


def create_app(
    *,
    jobs: JobQueue,
    tokens: TokenCodec,
    audits: AuditSink,
    fleet: Callable[[], Mapping[str, object]],
    now: Callable[[], int] = lambda: int(time.time()),
    admin: AdminServices | None = None,
) -> FastAPI:
    app = FastAPI(title="DGX Forge Control", version="1.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
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

    @app.post("/api/v1/reconciliations", status_code=status.HTTP_202_ACCEPTED)
    def reconcile(body: ChangeRequest, request: Request, authenticated: Actor = Depends(actor)) -> dict[str, object]:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        if admin is None or admin.reconciler is None:
            raise HTTPException(status_code=503, detail="reconciliation unavailable")
        return dict(admin.reconciler.enqueue(body.proposal_digest, authenticated.subject, request.state.request_id))

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

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    online_lock = OnlineLock(settings.state_path / "offline.lock")
    online_lock.__enter__()
    job_service = JobService(sessions, clock=clock)
    repository = RepositoryService(settings.repository_path)
    proposals = ProposalService(repository, head=repository.head)
    app = create_app(
        jobs=job_service,
        tokens=TokenCodec(settings.token_signing_key),
        audits=SqlAuditStore(sessions, clock),
        fleet=lambda: {"repository_path": str(settings.repository_path), "nodes": []},
        admin=AdminServices(repository, proposals, None, None),
    )
    @app.on_event("shutdown")
    def release_online_lock() -> None:
        online_lock.__exit__()
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
