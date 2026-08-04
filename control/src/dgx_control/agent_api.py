"""Strict human-enrollment and mTLS-authenticated agent API routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from dgx_agent_protocol import (
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import StreamingResponse

from .agent_jobs import AgentJobService, StaleAgentAttempt
from .auth import Actor, AgentIdentity, agent_identity_from_scope, agent_source_from_scope
from .enrollment import (
    EnrollmentDenied,
    EnrollmentService,
    PendingEnrollment,
    RemoteRevocationUncertain,
    RenewalInProgress,
)
from .models import AgentCertificate, AgentEnrollment, AgentNode, AgentOperation
from .pki import IssuedCertificate
from .presence import AgentPresenceService, PresenceError

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LIVE_OPERATION_STATES = frozenset({"queued", "running"})
_MAX_CSR_BYTES = 16 * 1024
_MAX_EVIDENCE_FIELDS = 8
_MAX_EVIDENCE_BYTES = 8 * 1024
_MAX_ENROLLMENT_BODY_BYTES = 64 * 1024
_MAX_ENROLLMENT_TOKEN_PREFIX_BYTES = 2 * 1024
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_RANGE_BYTES = 8 * 1024 * 1024


class _ActorDependency(Protocol):
    def __call__(self, request: Request) -> Actor: ...


@dataclass(frozen=True)
class AgentApiServices:
    enrollment: EnrollmentService
    operations: AgentJobService
    sessions: sessionmaker[Session]
    clock: Callable[[], datetime]
    presence: AgentPresenceService
    artifact_root: Path
    max_artifact_bytes: int = _MAX_ARTIFACT_BYTES
    max_range_bytes: int = _MAX_RANGE_BYTES


class EnrollmentRateLimiter:
    """Fixed global admission limit for unauthenticated enrollment bodies.

    The limiter intentionally has no client-keyed state: before enrollment a
    caller is unauthenticated, so attacker-chosen client addresses must not
    allocate unbounded memory. It is process-local; the deployment runs one
    control API instance behind the sole Caddy ingress boundary.
    """

    def __init__(
        self,
        *,
        maximum: int = 20,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum < 1 or window_seconds <= 0:
            raise ValueError("enrollment rate limit must be positive")
        self._maximum = maximum
        self._window_seconds = window_seconds
        self._clock = clock
        self._admitted: deque[float] = deque()
        self._lock = Lock()

    def admit(self) -> bool:
        now = self._clock()
        with self._lock:
            cutoff = now - self._window_seconds
            while self._admitted and self._admitted[0] <= cutoff:
                self._admitted.popleft()
            if len(self._admitted) >= self._maximum:
                return False
            self._admitted.append(now)
            return True


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    ttl_seconds: int = Field(ge=1, le=600)


class EnrollmentSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_token: str = Field(min_length=43, max_length=64)
    csr: str = Field(min_length=1, max_length=_MAX_CSR_BYTES)
    evidence: dict[str, str] = Field(min_length=6, max_length=_MAX_EVIDENCE_FIELDS)

    @field_validator("evidence")
    @classmethod
    def bounded_expected_evidence(cls, evidence: dict[str, str]) -> dict[str, str]:
        expected = {
            "node_id", "csr_public_key_fingerprint", "host_key_fingerprint",
            "hardware_fingerprint", "agent_digest", "boot_id",
        }
        if set(evidence) != expected or any(not value.strip() for value in evidence.values()):
            raise ValueError("evidence fields are invalid")
        if len(canonical_message(evidence)) > _MAX_EVIDENCE_BYTES:
            raise ValueError("evidence is too large")
        return evidence


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1024)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_seconds: int = Field(default=30, ge=1, le=300)
    node_id: str | None = Field(default=None, pattern=r"^spk_[0-9a-f]{32}$")
    wait_seconds: int = Field(default=0, ge=0, le=60)


class RenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csr: str = Field(min_length=1, max_length=_MAX_CSR_BYTES)
    node_id: str | None = Field(default=None, pattern=r"^spk_[0-9a-f]{32}$")


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1)
    node_id: str | None = Field(default=None, pattern=r"^spk_[0-9a-f]{32}$")


_DEFAULT_CLAIM_REQUEST = ClaimRequest()


def _wire(value: object) -> object:
    return json.loads(canonical_message(value))


def _now(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _issued_response(issued: object) -> dict[str, object]:
    return {
        "node_id": issued.node_id,
        "certificate_pem": issued.certificate_pem.decode("ascii"),
        "chain_pem": issued.chain_pem.decode("ascii"),
        "serial": issued.serial,
        "fingerprint": issued.fingerprint,
        "not_before": _now(issued.not_before).isoformat(),
        "not_after": _now(issued.not_after).isoformat(),
        "generation": issued.generation,
    }


def _json_response(value: object, *, status_code: int = 200) -> Response:
    return Response(
        content=canonical_message(value),
        status_code=status_code,
        media_type="application/json",
    )


def _require_services(services: AgentApiServices | None) -> AgentApiServices:
    if services is None:
        raise HTTPException(status_code=503, detail="agent API is unavailable")
    return services


def _require_administrator(actor: Actor, path: str) -> None:
    if actor.role != "administrator":
        raise HTTPException(status_code=403, detail="insufficient role")


def _scope_identity(request: Request) -> AgentIdentity:
    identity = agent_identity_from_scope(request.scope)
    if identity is None:
        raise HTTPException(status_code=401, detail="verified agent identity required")
    return identity


def active_agent_identity(services: AgentApiServices, identity: AgentIdentity | None) -> bool:
    return _agent_identity_state(services, identity) == "active"


def activation_agent_identity(services: AgentApiServices, identity: AgentIdentity | None) -> bool:
    return _agent_identity_state(services, identity) in {"active", "staged"}


def _agent_identity_state(
    services: AgentApiServices, identity: AgentIdentity | None
) -> str | None:
    if identity is None:
        return None
    now = _now(services.clock())
    with services.sessions() as session:
        valid = session.scalar(
            select(AgentCertificate.state)
            .join(AgentNode, AgentNode.node_id == AgentCertificate.node_id)
            .where(
                AgentCertificate.serial == identity.certificate_serial,
                AgentCertificate.node_id == identity.node_id,
                AgentCertificate.fingerprint == identity.certificate_fingerprint,
                AgentCertificate.revoked_at.is_(None),
                AgentCertificate.not_before <= now,
                AgentCertificate.not_after > now,
                AgentNode.state == "active",
                AgentNode.revoked_at.is_(None),
            )
        )
    return valid


def _authenticated_identity(request: Request, services: AgentApiServices) -> AgentIdentity:
    identity = _scope_identity(request)
    if not active_agent_identity(services, identity):
        raise HTTPException(status_code=401, detail="agent certificate is not active")
    return identity


def _authenticated_activation_identity(
    request: Request, services: AgentApiServices
) -> AgentIdentity:
    identity = _scope_identity(request)
    if not activation_agent_identity(services, identity):
        raise HTTPException(status_code=401, detail="agent certificate cannot activate")
    return identity


def _body_node_matches(value: str | None, identity: AgentIdentity) -> None:
    if value is not None and value != identity.node_id:
        raise HTTPException(status_code=403, detail="authenticated node identity cannot be overridden")


def _observed_agent_source(request: Request) -> str:
    source = agent_source_from_scope(request.scope)
    if source is None:
        raise HTTPException(
            status_code=422,
            detail="exactly one proxy-observed agent source is required",
        )
    return source


_ENROLLMENT_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_JSON_WHITESPACE = frozenset(b" \t\r\n")


@dataclass(frozen=True)
class _EnrollmentGrantScan:
    tokens: tuple[str, ...]
    top_level_keys: int


def _json_string_end(value: bytes | bytearray, start: int) -> int | None:
    """Return the exclusive end of one bounded JSON string literal."""
    index = start + 1
    while index < len(value):
        byte = value[index]
        if byte == ord('"'):
            return index + 1
        if byte == ord("\\"):
            index += 2
        else:
            index += 1
    return None


def _skip_json_whitespace(value: bytes | bytearray, start: int) -> int:
    while start < len(value) and value[start] in _JSON_WHITESPACE:
        start += 1
    return start


def _decode_bounded_json_string(
    value: bytes | bytearray,
    start: int,
    end: int,
    *,
    maximum_characters: int,
) -> str | None:
    # An ASCII target cannot require more than one six-byte \uXXXX escape per
    # character.  Reject longer candidates before making even a bounded copy.
    if end - start > 2 + (6 * maximum_characters):
        return None
    try:
        decoded = json.loads(bytes(value[start:end]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, str) or len(decoded) > maximum_characters:
        return None
    return decoded


def _scan_enrollment_grants(value: bytes | bytearray) -> _EnrollmentGrantScan:
    """Discover bounded grant strings without recursively parsing the body."""
    tokens: list[str] = []
    seen: set[str] = set()
    top_level_keys = 0
    root_container: int | None = None
    depth = 0
    index = 0
    while index < len(value):
        byte = value[index]
        if byte == ord('"'):
            end = _json_string_end(value, index)
            if end is None:
                break
            colon = _skip_json_whitespace(value, end)
            if colon < len(value) and value[colon] == ord(":"):
                key = _decode_bounded_json_string(
                    value, index, end, maximum_characters=len("grant_token")
                )
                if key == "grant_token":
                    if root_container == ord("{") and depth == 1:
                        top_level_keys += 1
                    token_start = _skip_json_whitespace(value, colon + 1)
                    if token_start < len(value) and value[token_start] == ord('"'):
                        token_end = _json_string_end(value, token_start)
                        if token_end is not None:
                            token = _decode_bounded_json_string(
                                value,
                                token_start,
                                token_end,
                                maximum_characters=43,
                            )
                            if (
                                token is not None
                                and _ENROLLMENT_TOKEN.fullmatch(token) is not None
                                and token not in seen
                            ):
                                seen.add(token)
                                tokens.append(token)
            index = end
            continue
        if byte in (ord("{"), ord("[")):
            if root_container is None and depth == 0:
                root_container = byte
            depth += 1
        elif byte in (ord("}"), ord("]")) and depth > 0:
            depth -= 1
        index += 1
    return _EnrollmentGrantScan(tuple(tokens), top_level_keys)


def _consume_enrollment_denial(services: AgentApiServices, tokens: tuple[str, ...]) -> None:
    for token in tokens:
        try:
            services.enrollment.submit(token, b"", {})
        except EnrollmentDenied:
            pass


async def _bounded_enrollment_body(request: Request, services: AgentApiServices) -> bytearray:
    buffered = bytearray()
    token_prefix = bytearray()
    async for chunk in request.stream():
        prefix_remaining = _MAX_ENROLLMENT_TOKEN_PREFIX_BYTES - len(token_prefix)
        if prefix_remaining > 0:
            token_prefix.extend(chunk[:prefix_remaining])
        remaining = _MAX_ENROLLMENT_BODY_BYTES - len(buffered)
        if len(chunk) > remaining:
            scan = _scan_enrollment_grants(token_prefix)
            _consume_enrollment_denial(services, scan.tokens)
            raise HTTPException(status_code=413, detail="enrollment request is too large")
        buffered.extend(chunk)
    return buffered


def _enrollment_view(enrollment: AgentEnrollment) -> dict[str, object]:
    return {
        "id": enrollment.id,
        "node_id": enrollment.node_id,
        "state": enrollment.state,
        "csr_public_key_fingerprint": enrollment.csr_public_key_fingerprint,
        "host_key_fingerprint": enrollment.host_key_fingerprint,
        "hardware_fingerprint": enrollment.hardware_fingerprint,
        "agent_digest": enrollment.agent_digest,
        "boot_id": enrollment.boot_id,
        "created_at": _now(enrollment.created_at).isoformat(),
        "decision_actor": enrollment.decision_actor,
        "decided_at": _now(enrollment.decided_at).isoformat() if enrollment.decided_at else None,
        "rejection_reason": enrollment.rejection_reason,
        "certificate_serial": enrollment.certificate_serial,
        "certificate_fingerprint": enrollment.certificate_fingerprint,
    }


def _references_digest(value: object, digest: str) -> bool:
    if isinstance(value, str):
        return value == digest
    if isinstance(value, Mapping):
        return any(_references_digest(item, digest) for item in value.values())
    if isinstance(value, list):
        return any(_references_digest(item, digest) for item in value)
    return False


def _open_owned_artifact(services: AgentApiServices, identity: AgentIdentity, digest: str) -> tuple[int, int]:
    if _DIGEST.fullmatch(digest) is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    with services.sessions() as session:
        operations = list(session.scalars(
            select(AgentOperation).where(
                AgentOperation.node_id == identity.node_id,
                AgentOperation.state.in_(_LIVE_OPERATION_STATES),
            )
        ))
    if not any(_references_digest(operation.payload, digest) for operation in operations):
        raise HTTPException(status_code=404, detail="artifact not found")
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(os.fspath(services.artifact_root), root_flags)
        try:
            descriptor = os.open(digest, file_flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
    except OSError:
        raise HTTPException(status_code=404, detail="artifact not found") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > services.max_artifact_bytes:
            raise HTTPException(status_code=404 if not stat.S_ISREG(metadata.st_mode) else 413, detail="artifact not available")
        return descriptor, metadata.st_size
    except Exception:
        os.close(descriptor)
        raise


def _range(value: str | None, total: int, maximum: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d+)-(\d+)", value)
    if match is None:
        raise HTTPException(status_code=416, detail="range is invalid")
    if any(len(part) > 19 for part in match.groups()):
        raise HTTPException(status_code=416, detail="range is invalid")
    try:
        start, end = (int(part) for part in match.groups())
    except ValueError:
        raise HTTPException(status_code=416, detail="range is invalid") from None
    if start > end or start >= total or end >= total or end - start + 1 > maximum:
        raise HTTPException(status_code=416, detail="range is invalid")
    return start, end


def _read_chunks(descriptor: int, start: int, length: int):
    try:
        os.lseek(descriptor, start, os.SEEK_SET)
        remaining = length
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(descriptor)


def _sealed_snapshot(descriptor: int, size: int, maximum: int, digest: str):
    snapshot = None
    try:
        # Ownership transfers to _SnapshotResponse, which closes after send.
        snapshot = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
        copied = 0
        content_hash = hashlib.sha256()
        while copied < size:
            chunk = os.read(descriptor, min(64 * 1024, size - copied))
            if not chunk:
                raise HTTPException(status_code=404, detail="artifact changed during read")
            copied += len(chunk)
            if copied > maximum:
                raise HTTPException(status_code=413, detail="artifact not available")
            content_hash.update(chunk)
            snapshot.write(chunk)
        after = os.fstat(descriptor)
        if after.st_size != size or os.read(descriptor, 1):
            raise HTTPException(status_code=404, detail="artifact changed during read")
        if not hmac.compare_digest(content_hash.hexdigest(), digest):
            raise HTTPException(status_code=404, detail="artifact not found")
        snapshot.seek(0)
        return snapshot
    except Exception:
        if snapshot is not None:
            snapshot.close()
        raise
    finally:
        os.close(descriptor)


class _SnapshotResponse(StreamingResponse):
    def __init__(self, snapshot, start: int, length: int, **kwargs: object) -> None:
        self._snapshot = snapshot
        super().__init__(self._chunks(start, length), **kwargs)

    def _chunks(self, start: int, length: int):
        self._snapshot.seek(start)
        remaining = length
        while remaining:
            chunk = self._snapshot.read(min(64 * 1024, remaining))
            if not chunk:
                raise RuntimeError("sealed artifact snapshot was truncated")
            remaining -= len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._snapshot.close()


def install_agent_routes(
    app: Any,
    *,
    actor_dependency: _ActorDependency,
    services: AgentApiServices | None,
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
) -> None:
    human = APIRouter(prefix="/api/v1/agents")
    agent = APIRouter(prefix="/agent/v1")
    limiter = enrollment_rate_limiter or EnrollmentRateLimiter()
    authenticated_actor = Depends(actor_dependency)

    @human.post("/enrollments/grants", status_code=status.HTTP_201_CREATED)
    def create_grant(body: GrantRequest, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        _require_administrator(authenticated, "/api/v1/agents/enrollments/grants")
        required = _require_services(services)
        try:
            grant = required.enrollment.create(body.node_id, authenticated.subject, body.ttl_seconds)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {"id": grant.id, "node_id": grant.node_id, "expires_at": _now(grant.expires_at).isoformat(), "token": grant.token}

    @human.get("/enrollments")
    def list_enrollments(
        cursor: str | None = None,
        state: str | None = None,
        limit: int = 100,
        authenticated: Actor = authenticated_actor,
    ) -> dict[str, object]:
        _require_administrator(authenticated, "/api/v1/agents/enrollments")
        required = _require_services(services)
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be between one and 100")
        with required.sessions() as session:
            statement = select(AgentEnrollment)
            if state is not None:
                statement = statement.where(AgentEnrollment.state == state)
            if cursor is not None:
                cursor_record = session.get(AgentEnrollment, cursor)
                if cursor_record is None:
                    raise HTTPException(status_code=422, detail="cursor is invalid")
                statement = statement.where(or_(
                    AgentEnrollment.created_at < cursor_record.created_at,
                    and_(AgentEnrollment.created_at == cursor_record.created_at, AgentEnrollment.id < cursor_record.id),
                ))
            records = list(session.scalars(statement.order_by(AgentEnrollment.created_at.desc(), AgentEnrollment.id.desc()).limit(limit + 1)))
        # In particular, an uncertain `issuing` record remains visible here;
        # this endpoint intentionally never retries or clears it.
        page = records[:limit]
        return {
            "enrollments": [_enrollment_view(record) for record in page],
            "next_cursor": page[-1].id if len(records) > limit and page else None,
        }

    @human.post("/enrollments/{enrollment_id}/approve")
    def approve(enrollment_id: str, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        _require_administrator(authenticated, "/api/v1/agents/enrollments/{enrollment_id}/approve")
        required = _require_services(services)
        try:
            return _issued_response(required.enrollment.approve(enrollment_id, authenticated.subject))
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @human.post("/enrollments/{enrollment_id}/reject")
    def reject(enrollment_id: str, body: RejectRequest, authenticated: Actor = authenticated_actor) -> dict[str, object]:
        _require_administrator(authenticated, "/api/v1/agents/enrollments/{enrollment_id}/reject")
        required = _require_services(services)
        try:
            record = required.enrollment.reject(enrollment_id, authenticated.subject, body.reason)
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return {"id": record.id, "node_id": record.node_id, "state": record.state}

    @human.post("/nodes/{node_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
    def revoke(node_id: str, authenticated: Actor = authenticated_actor) -> Response:
        _require_administrator(authenticated, "/api/v1/agents/nodes/{node_id}/revoke")
        required = _require_services(services)
        try:
            required.enrollment.revoke_node(node_id, authenticated.subject)
        except RemoteRevocationUncertain as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except EnrollmentDenied as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post("/enroll", status_code=status.HTTP_202_ACCEPTED)
    async def enroll(request: Request) -> Response:
        required = _require_services(services)
        if not limiter.admit():
            raise HTTPException(status_code=429, detail="enrollment rate limit exceeded")
        raw = await _bounded_enrollment_body(request, required)
        scan = _scan_enrollment_grants(raw)
        content_type = request.headers.get("content-type", "")
        if re.fullmatch(r"application/json(?:\s*;\s*charset=(?:utf-8|utf8))?", content_type, re.IGNORECASE) is None:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=415, detail="enrollment content type must be application/json")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (TypeError, UnicodeDecodeError, ValueError, RecursionError):
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=422, detail="enrollment request must be JSON") from None
        if not isinstance(body, dict):
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=422, detail="enrollment request must be a JSON object")
        if scan.top_level_keys != 1:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=422, detail="enrollment grant is ambiguous")
        if not isinstance(body.get("grant_token"), str):
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=422, detail="enrollment grant is required")
        csr = body.get("csr")
        evidence = body.get("evidence")
        try:
            csr_bytes = csr.encode("ascii") if isinstance(csr, str) else b""
        except UnicodeEncodeError:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=422, detail="CSR must be ASCII PEM") from None
        service_evidence = (
            evidence
            if isinstance(evidence, Mapping) and set(body) == {"grant_token", "csr", "evidence"}
            else {}
        )
        try:
            outcome = required.enrollment.submit(body["grant_token"], csr_bytes, service_evidence)
        except EnrollmentDenied as error:
            _consume_enrollment_denial(required, scan.tokens)
            raise HTTPException(status_code=403, detail=str(error)) from None
        if isinstance(outcome, IssuedCertificate):
            return _json_response(_issued_response(outcome))
        assert isinstance(outcome, PendingEnrollment)
        return _json_response(
            {"id": outcome.id, "node_id": outcome.node_id, "state": outcome.state},
            status_code=status.HTTP_202_ACCEPTED,
        )

    @agent.post("/claim")
    def claim(request: Request, body: ClaimRequest = _DEFAULT_CLAIM_REQUEST) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            required.presence.observe(
                identity.node_id,
                _observed_agent_source(request),
                required.clock(),
            )
        except PresenceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        try:
            result = required.operations.claim(
                identity.node_id,
                identity.certificate_serial,
                body.lease_seconds,
                body.wait_seconds,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT) if result is None else _json_response(_wire(result))

    @agent.post("/heartbeat")
    def heartbeat(body: dict[str, object], request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        try:
            message = AgentProgress.parse(body)
        except AgentProtocolError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        _body_node_matches(message.node_id, identity)
        try:
            return _json_response(_wire(required.operations.heartbeat(message, message.progress, 30)))
        except (StaleAgentAttempt, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @agent.post("/result", status_code=status.HTTP_204_NO_CONTENT)
    def result(body: dict[str, object], request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        try:
            message = AgentResult.parse(body)
        except AgentProtocolError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        _body_node_matches(message.node_id, identity)
        try:
            if message.state == "succeeded":
                required.operations.succeed(message, message.result)
            elif message.state == "failed":
                error_code = message.result.get("error_code")
                if (
                    message.result.get("status") != "failed"
                    or not isinstance(error_code, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None
                ):
                    raise ValueError("stable failure error code is required")
                required.operations.fail(message, error_code)
            else:
                required.operations.wait_for_operator(message, str(message.result.get("reason", "")))
        except (StaleAgentAttempt, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.post("/renew")
    def renew(body: RenewRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            issued = required.enrollment.renew(identity.node_id, identity.certificate_serial, body.csr.encode("ascii"))
        except UnicodeEncodeError:
            raise HTTPException(status_code=422, detail="CSR must be ASCII PEM") from None
        except RenewalInProgress as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return _json_response(_issued_response(issued))

    @agent.post("/renew/activate", status_code=status.HTTP_204_NO_CONTENT)
    def activate(body: ActivateRequest, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_activation_identity(request, required)
        _body_node_matches(body.node_id, identity)
        try:
            required.enrollment.activate(
                identity.node_id,
                identity.certificate_serial,
                body.generation,
            )
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @agent.get("/artifacts/{sha256}")
    def artifact(sha256: str, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        identity = _authenticated_identity(request, required)
        descriptor, size = _open_owned_artifact(required, identity, sha256)
        try:
            requested = _range(request.headers.get("range"), size, required.max_range_bytes)
        except Exception:
            os.close(descriptor)
            raise
        if requested is None:
            start, end, code = 0, size - 1, status.HTTP_200_OK
        else:
            start, end, code = requested[0], requested[1], status.HTTP_206_PARTIAL_CONTENT
        length = end - start + 1
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
        if code == status.HTTP_206_PARTIAL_CONTENT:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        snapshot = _sealed_snapshot(descriptor, size, required.max_artifact_bytes, sha256)
        return _SnapshotResponse(snapshot, start, length, status_code=code, headers=headers, media_type="application/octet-stream")

    app.include_router(human)
    app.include_router(agent)
