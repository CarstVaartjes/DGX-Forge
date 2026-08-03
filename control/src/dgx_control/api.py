"""Versioned authenticated control API."""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditRecord
from .auth import Actor, AuthError, TokenCodec


class JobQueue(Protocol):
    def enqueue(self, kind: str, actor: str, base_commit: str, targets: Sequence[str], payload: Mapping[str, object], *, request_id: str) -> Any: ...
    def get(self, job_id: str) -> Any: ...


class AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=80)
    base_commit: str = Field(min_length=1, max_length=128)
    targets: list[str] = Field(max_length=64)
    payload: dict[str, object]


class JobResponse(BaseModel):
    id: str
    state: str


def create_app(
    *,
    jobs: JobQueue,
    tokens: TokenCodec,
    audits: AuditSink,
    fleet: Callable[[], Mapping[str, object]],
    now: Callable[[], int] = lambda: int(time.time()),
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

    @app.post("/api/v1/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
    def enqueue(body: JobRequest, request: Request, authenticated: Actor = Depends(actor)) -> JobResponse:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")
        job = jobs.enqueue(body.kind, authenticated.subject, body.base_commit, body.targets, body.payload, request_id=request.state.request_id)
        audits.append(AuditRecord(request.state.request_id, authenticated.subject, f"job.enqueue:{body.kind}", body.base_commit, tuple(body.targets)))
        return JobResponse(id=str(job.id), state=str(job.state))

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
    from .settings import Settings

    settings = Settings.from_env_and_secrets()
    sessions = session_factory(build_engine(settings.database_url))
    clock = lambda: datetime.now(UTC)
    return create_app(
        jobs=JobService(sessions, clock=clock),
        tokens=TokenCodec(settings.token_signing_key),
        audits=SqlAuditStore(sessions, clock),
        fleet=lambda: {"repository_path": str(settings.repository_path), "nodes": []},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
