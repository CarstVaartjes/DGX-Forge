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
from typing import Any, Literal, Protocol

from dgx_agent_protocol import (
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)
from dgx_agent_protocol.workload_packages import (
    PackageHelperOperation,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
)
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import StreamingResponse

from .agent_jobs import AgentJobService, StaleAgentAttempt
from .audit import AuditRecord
from .auth import (
    Actor,
    AgentIdentity,
    AgentSource,
    agent_identity_from_scope,
    agent_source_from_scope,
)
from .enrollment import (
    EnrollmentDenied,
    EnrollmentService,
    PendingEnrollment,
    RemoteRevocationUncertain,
    RenewalInProgress,
)
from .models import AgentCertificate, AgentEnrollment, AgentNode, AgentOperation
from .package_helper_authority import PackageHelperAuthorityError, PackageHelperAuthorityService
from .operation_api import bounded_error_responses
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
_MAX_TUF_METADATA_BYTES = 2 * 1024 * 1024
_MAX_TUF_TARGET_BYTES = 16 * 1024 * 1024
_TUF_METADATA_NAME = re.compile(
    r"(?:[1-9][0-9]*\.root|timestamp|snapshot|targets|"
    r"[a-z0-9][a-z0-9._-]{0,126})\.json\Z"
)
_TUF_PLATFORM_TARGET_NAME = re.compile(
    r"platform/releases/"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"[0-9a-f]{64}\.json\Z"
)
_WORKLOAD_TUF_METADATA_NAME = re.compile(
    r"(?:[1-9][0-9]*\.root|timestamp|snapshot|targets|families|releases|"
    r"[1-9][0-9]*\.(?:targets|families|releases))\.json\Z"
)
_WORKLOAD_TUF_TARGET_NAME = re.compile(r"releases/[0-9a-f]{64}\.json\Z")


class _ActorDependency(Protocol):
    def __call__(self, request: Request) -> Actor: ...


class _AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...


@dataclass(frozen=True)
class AgentApiServices:
    enrollment: EnrollmentService
    operations: AgentJobService
    sessions: sessionmaker[Session]
    clock: Callable[[], datetime]
    presence: AgentPresenceService
    artifact_root: Path
    tuf_metadata_root: Path = Path("/state/agent-tuf/metadata")
    tuf_target_root: Path = Path("/state/agent-tuf/targets")
    workload_tuf_metadata_root: Path = Path("/state/workload-tuf/metadata")
    workload_tuf_target_root: Path = Path("/state/workload-tuf/targets")
    max_artifact_bytes: int = _MAX_ARTIFACT_BYTES
    max_range_bytes: int = _MAX_RANGE_BYTES
    max_tuf_metadata_bytes: int = _MAX_TUF_METADATA_BYTES
    max_tuf_target_bytes: int = _MAX_TUF_TARGET_BYTES
    max_workload_tuf_metadata_bytes: int = 2 * 1024 * 1024
    max_workload_tuf_target_bytes: int = 1024 * 1024
    package_helper_authority: PackageHelperAuthorityService | None = None


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


class EnrollmentDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    state: Literal["approved", "rejected"]


class EnrollmentGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    expires_at: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=43, max_length=64)


class EnrollmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    state: str = Field(min_length=1, max_length=32)
    csr_public_key_fingerprint: str = Field(min_length=1, max_length=512)
    host_key_fingerprint: str = Field(min_length=1, max_length=512)
    hardware_fingerprint: str = Field(min_length=1, max_length=512)
    agent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    boot_id: str = Field(min_length=1, max_length=512)
    created_at: str = Field(min_length=1, max_length=64)
    decision_actor: str | None = Field(default=None, max_length=200)
    decided_at: str | None = Field(default=None, max_length=64)
    rejection_reason: str | None = Field(default=None, max_length=1024)
    certificate_serial: str | None = Field(default=None, max_length=256)
    certificate_fingerprint: str | None = Field(default=None, max_length=512)


class EnrollmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollments: list[EnrollmentSummary] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=128)


class AgentRuntimeIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    architecture: Literal["linux-arm64", "linux-x86_64"]
    platform_version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    build_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    active_slot: str = Field(pattern=r"^[AB]$")
    agent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supervisor_generation: int = Field(ge=1, le=999_999_999, strict=True)
    supervisor_ready_generation: int | None = Field(
        default=None, ge=1, le=999_999_999, strict=True
    )
    self_test_passed: bool = Field(default=False, strict=True)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_seconds: int = Field(default=30, ge=1, le=300)
    node_id: str | None = Field(default=None, pattern=r"^spk_[0-9a-f]{32}$")
    protocol_version: int = Field(default=1, ge=1, le=2_147_483_647, strict=True)
    capabilities: list[str] | None = Field(default=None, max_length=32)
    runtime_identity: AgentRuntimeIdentityRequest | None = None
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


def _validated_authenticated_source(
    request: Request,
    services: AgentApiServices,
    identity: AgentIdentity,
) -> AgentSource:
    source = agent_source_from_scope(request.scope)
    if source is None or source.identity != identity:
        raise HTTPException(status_code=401, detail="verified agent source required")
    try:
        return services.presence.validate(source)
    except PresenceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


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


def _read_tuf_file(root: Path, name: str, maximum: int) -> bytes:
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    components = name.split("/")
    if not components or any(component in {"", ".", ".."} for component in components):
        raise HTTPException(status_code=404, detail="TUF file not found")
    directory_descriptor = -1
    try:
        root_metadata = root.lstat()
        if (
            not root.is_absolute()
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid not in {0, os.geteuid()}
            or root_metadata.st_mode & 0o022
        ):
            raise OSError("unsafe TUF root")
        directory_descriptor = os.open(os.fspath(root), root_flags)
        try:
            opened_root = os.fstat(directory_descriptor)
            root_identity = lambda item: (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
            )
            if root_identity(root_metadata) != root_identity(opened_root):
                raise OSError("TUF root changed")
            for component in components[:-1]:
                nested_descriptor = os.open(
                    component,
                    root_flags,
                    dir_fd=directory_descriptor,
                )
                try:
                    nested = os.fstat(nested_descriptor)
                    if (
                        not stat.S_ISDIR(nested.st_mode)
                        or nested.st_uid not in {0, os.geteuid()}
                        or nested.st_mode & 0o022
                    ):
                        raise OSError("unsafe TUF directory")
                except Exception:
                    os.close(nested_descriptor)
                    raise
                os.close(directory_descriptor)
                directory_descriptor = nested_descriptor
            descriptor = os.open(
                components[-1],
                file_flags,
                dir_fd=directory_descriptor,
            )
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
    except OSError:
        raise HTTPException(status_code=404, detail="TUF file not found") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & 0o022
            or before.st_mode & 0o111
        ):
            raise HTTPException(status_code=404, detail="TUF file not found")
        if not 0 < before.st_size <= maximum:
            raise HTTPException(
                status_code=413 if before.st_size > maximum else 404,
                detail="TUF file is unavailable",
            )
        remaining = before.st_size
        chunks: list[bytes] = []
        first_digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise HTTPException(status_code=404, detail="TUF file changed")
            chunks.append(chunk)
            first_digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if identity(before) != identity(after) or os.read(descriptor, 1):
            raise HTTPException(status_code=404, detail="TUF file changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        remaining = before.st_size
        second_digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise HTTPException(status_code=404, detail="TUF file changed")
            second_digest.update(chunk)
            remaining -= len(chunk)
        rechecked = os.fstat(descriptor)
        if (
            not hmac.compare_digest(first_digest.digest(), second_digest.digest())
            or identity(after) != identity(rechecked)
            or os.read(descriptor, 1)
        ):
            raise HTTPException(status_code=404, detail="TUF file changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


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
    audits: _AuditSink,
    services: AgentApiServices | None,
    enrollment_rate_limiter: EnrollmentRateLimiter | None = None,
) -> None:
    human = APIRouter(prefix="/api/v1/agents")
    agent = APIRouter(prefix="/agent/v1")
    limiter = enrollment_rate_limiter or EnrollmentRateLimiter()
    authenticated_actor = Depends(actor_dependency)

    @human.post(
        "/enrollments/grants",
        status_code=status.HTTP_201_CREATED,
        response_model=EnrollmentGrantResponse,
        responses=bounded_error_responses(401, 403, 503),
    )
    def create_grant(
        body: GrantRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> EnrollmentGrantResponse:
        _require_administrator(authenticated, "/api/v1/agents/enrollments/grants")
        required = _require_services(services)
        try:
            grant = required.enrollment.create(body.node_id, authenticated.subject, body.ttl_seconds)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "agent.enrollment.grant.create",
                None,
                (grant.node_id,),
            )
        )
        return EnrollmentGrantResponse(
            id=grant.id,
            node_id=grant.node_id,
            expires_at=_now(grant.expires_at).isoformat(),
            token=grant.token,
        )

    @human.get(
        "/enrollments",
        response_model=EnrollmentListResponse,
        responses=bounded_error_responses(401, 403, 503),
    )
    def list_enrollments(
        cursor: str | None = None,
        state: str | None = None,
        limit: int = 100,
        authenticated: Actor = authenticated_actor,
    ) -> EnrollmentListResponse:
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
        return EnrollmentListResponse(
            enrollments=[
                EnrollmentSummary.model_validate(_enrollment_view(record))
                for record in page
            ],
            next_cursor=(
                page[-1].id if len(records) > limit and page else None
            ),
        )

    @human.post(
        "/enrollments/{enrollment_id}/approve",
        response_model=EnrollmentDecisionResponse,
        responses=bounded_error_responses(401, 403, 409, 503),
    )
    def approve(
        enrollment_id: str,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> EnrollmentDecisionResponse:
        _require_administrator(authenticated, "/api/v1/agents/enrollments/{enrollment_id}/approve")
        required = _require_services(services)
        try:
            issued = required.enrollment.approve(
                enrollment_id, authenticated.subject
            )
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "agent.enrollment.approve",
                None,
                (enrollment_id, issued.node_id),
            )
        )
        return EnrollmentDecisionResponse(
            id=enrollment_id,
            node_id=issued.node_id,
            state="approved",
        )

    @human.post(
        "/enrollments/{enrollment_id}/reject",
        response_model=EnrollmentDecisionResponse,
        responses=bounded_error_responses(401, 403, 409, 503),
    )
    def reject(
        enrollment_id: str,
        body: RejectRequest,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> EnrollmentDecisionResponse:
        _require_administrator(authenticated, "/api/v1/agents/enrollments/{enrollment_id}/reject")
        required = _require_services(services)
        try:
            record = required.enrollment.reject(enrollment_id, authenticated.subject, body.reason)
        except (EnrollmentDenied, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "agent.enrollment.reject",
                None,
                (record.id, record.node_id),
            )
        )
        return EnrollmentDecisionResponse(
            id=record.id,
            node_id=record.node_id,
            state="rejected",
        )

    @human.post(
        "/nodes/{node_id}/revoke",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=bounded_error_responses(401, 403, 404, 503),
    )
    def revoke(
        node_id: str,
        request: Request,
        authenticated: Actor = authenticated_actor,
    ) -> Response:
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
        audits.append(
            AuditRecord(
                request.state.request_id,
                authenticated.subject,
                "agent.node.revoke",
                None,
                (node_id,),
            )
        )
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
        source = _validated_authenticated_source(request, required, identity)
        try:
            result = required.operations.claim(
                identity.node_id,
                identity.certificate_serial,
                body.lease_seconds,
                body.wait_seconds,
                body.protocol_version,
                body.capabilities,
                None
                if body.runtime_identity is None
                else body.runtime_identity.model_dump(),
                source=source,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT) if result is None else _json_response(_wire(result))

    def package_helper_service() -> PackageHelperAuthorityService:
        required = services.package_helper_authority if services is not None else None
        if required is None:
            raise HTTPException(status_code=503, detail="package helper authority unavailable")
        return required

    def package_helper_identity(request: Request) -> AgentIdentity:
        _scope_identity(request)
        required = _require_services(services)
        return _authenticated_identity(request, required)

    @agent.post("/package-helper/receipts")
    def package_helper_receipts(
        body: dict[str, object], request: Request
    ) -> Response:
        identity = package_helper_identity(request)
        required = package_helper_service()
        try:
            receipts = required.issue_receipts(
                node_id=body["node_id"],
                job_id=body["job_id"],
                operation_id=body["operation_id"],
                attempt=body["attempt"],
                fence=body["fence"],
                release_digest=body["release_digest"],
                objects=body["objects"],
                certificate_serial=identity.certificate_serial,
            )
            return _json_response(
                {"receipts": [item.to_mapping() for item in receipts]}
            )
        except (KeyError, TypeError, ValueError, PackageHelperAuthorityError):
            raise HTTPException(status_code=409, detail="package helper authority rejected request") from None

    @agent.post("/package-helper/grant")
    def package_helper_grant(
        body: dict[str, object], request: Request
    ) -> Response:
        identity = package_helper_identity(request)
        required = package_helper_service()
        try:
            operation = PackageHelperOperation(body["operation"])
            grant = required.issue_grant(
                request_id=body["request_id"],
                node_id=body["node_id"],
                job_id=body["job_id"],
                operation_id=body["operation_id"],
                attempt=body["attempt"],
                fence=body["fence"],
                release_digest=body["release_digest"],
                generation=body["generation"],
                operation=operation,
                request_digest=body["request_digest"],
                certificate_serial=identity.certificate_serial,
                expires_in_seconds=body.get("expires_in_seconds", 30),
            )
            return _json_response({"grant": grant.to_mapping()})
        except (KeyError, TypeError, ValueError, PackageHelperAuthorityError):
            raise HTTPException(status_code=409, detail="package helper authority rejected request") from None

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
        source = _validated_authenticated_source(request, required, identity)
        try:
            response = required.operations.heartbeat(
                message,
                message.progress,
                30,
                source=source,
            )
        except (StaleAgentAttempt, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return _json_response(_wire(response))

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
        source = _validated_authenticated_source(request, required, identity)
        try:
            if message.state == "failed":
                error_code = message.result.get("error_code")
                if (
                    message.result.get("status") != "failed"
                    or not isinstance(error_code, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None
                ):
                    raise ValueError("stable failure error code is required")
            required.operations.record_result(message, source=source)
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

    @agent.get("/tuf/metadata/{name}")
    def tuf_metadata(name: str, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        _authenticated_identity(request, required)
        if _TUF_METADATA_NAME.fullmatch(name) is None:
            raise HTTPException(status_code=404, detail="TUF file not found")
        raw = _read_tuf_file(
            required.tuf_metadata_root,
            name,
            required.max_tuf_metadata_bytes,
        )
        return Response(
            content=raw,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "Content-Length": str(len(raw))},
        )

    @agent.get("/tuf/targets/{name:path}")
    def tuf_target(name: str, request: Request) -> Response:
        _scope_identity(request)
        required = _require_services(services)
        _authenticated_identity(request, required)
        if _TUF_PLATFORM_TARGET_NAME.fullmatch(name) is None:
            raise HTTPException(status_code=404, detail="TUF file not found")
        raw = _read_tuf_file(
            required.tuf_target_root,
            name,
            required.max_tuf_target_bytes,
        )
        return Response(
            content=raw,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "Content-Length": str(len(raw))},
        )

    @agent.get("/workload-tuf/metadata/{name}")
    def workload_tuf_metadata(name: str, request: Request) -> Response:
        """Deliver only workload trust metadata over the node mTLS boundary."""
        _scope_identity(request)
        required = _require_services(services)
        _authenticated_identity(request, required)
        if _WORKLOAD_TUF_METADATA_NAME.fullmatch(name) is None:
            raise HTTPException(status_code=404, detail="workload TUF file not found")
        raw = _read_tuf_file(
            required.workload_tuf_metadata_root,
            name,
            required.max_workload_tuf_metadata_bytes,
        )
        return Response(
            content=raw,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "Content-Length": str(len(raw))},
        )

    @agent.get("/workload-tuf/targets/{name:path}")
    def workload_tuf_target(name: str, request: Request) -> Response:
        """Deliver one digest-addressed workload lock, never model payloads."""
        _scope_identity(request)
        required = _require_services(services)
        _authenticated_identity(request, required)
        if _WORKLOAD_TUF_TARGET_NAME.fullmatch(name) is None:
            raise HTTPException(status_code=404, detail="workload TUF target not found")
        digest = name.removeprefix("releases/").removesuffix(".json")
        raw = _read_tuf_file(
            required.workload_tuf_target_root,
            digest,
            required.max_workload_tuf_target_bytes,
        )
        if hashlib.sha256(raw).hexdigest() != digest:
            raise HTTPException(status_code=404, detail="workload TUF target not found")
        return Response(
            content=raw,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "Content-Length": str(len(raw))},
        )

    app.include_router(human)
    app.include_router(agent)
