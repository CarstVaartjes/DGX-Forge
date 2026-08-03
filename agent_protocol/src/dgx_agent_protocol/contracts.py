from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID


MAX_DOCUMENT_BYTES = 64 * 1024
NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
UNSAFE_KEY = re.compile(
    r"password|secret|token|authorization|private.?key|command|shell|environment",
    re.IGNORECASE,
)
PATH_KEY = re.compile(
    r"(?:^|[_-])(?:path|file|directory|filesystem|mount)(?:$|[_-])|(?:path|file|directory|filesystem|mount)$",
    re.IGNORECASE,
)


class AgentProtocolError(ValueError):
    """A protocol message is invalid or outside the agent trust boundary."""


class AgentOperation(StrEnum):
    NODE_PROBE = "node.probe"
    RELEASE_INSTALL = "release.install"
    WORKLOAD_PREPARE = "workload.prepare"
    WORKLOAD_START = "workload.start"
    WORKLOAD_STOP = "workload.stop"
    WORKLOAD_HEALTH = "workload.health"
    WORKLOAD_VERIFY = "workload.verify"
    AGENT_UPDATE = "agent.update"
    AGENT_ROLLBACK = "agent.rollback"


def canonical_message(value: Any) -> bytes:
    """Encode a protocol value with deterministic UTF-8 JSON."""
    try:
        return json.dumps(
            _to_wire(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AgentProtocolError("message must contain JSON values") from error


def _to_wire(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _to_wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_wire(item) for item in value]
    return value


def _canonical_copy(value: Any, *, name: str) -> Any:
    try:
        copied = json.loads(canonical_message(value))
    except AgentProtocolError as error:
        raise AgentProtocolError(f"{name} must be JSON") from error
    return _freeze(copied)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_safe_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgentProtocolError("JSON object keys must be strings")
            if PATH_KEY.search(key):
                raise AgentProtocolError(f"filesystem path key is not allowed: {key}")
            if UNSAFE_KEY.search(key):
                raise AgentProtocolError(f"unsafe protocol key: {key}")
            _validate_safe_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_safe_keys(item)
    elif isinstance(value, str) and ("/" in value or "\\" in value):
        raise AgentProtocolError("filesystem path values are not allowed")


def _validate_bounded_document(value: Any, *, name: str) -> Any:
    if not isinstance(value, Mapping):
        raise AgentProtocolError(f"{name} must be a JSON object")
    _validate_safe_keys(value)
    copied = _canonical_copy(value, name=name)
    if len(canonical_message(copied)) > MAX_DOCUMENT_BYTES:
        raise AgentProtocolError(f"{name} is too large")
    return copied


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentProtocolError("message must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise AgentProtocolError("JSON object keys must be strings")
    return value


def _fields(value: Mapping[str, Any], *, required: set[str]) -> None:
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        detail = f"missing fields: {', '.join(missing)}" if missing else f"unknown fields: {', '.join(unknown)}"
        raise AgentProtocolError(detail)


def _version(value: Any) -> int:
    if value != 1 or isinstance(value, bool):
        raise AgentProtocolError("unsupported schema_version")
    return 1


def _uuid(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise AgentProtocolError(f"{name} must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise AgentProtocolError(f"{name} must be a UUID") from error
    if str(parsed) != value:
        raise AgentProtocolError(f"{name} must be a canonical UUID")
    return value


def _attempt(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AgentProtocolError("attempt must be a positive integer")
    return value


def _node_id(value: Any) -> str:
    if not isinstance(value, str) or not NODE_ID.fullmatch(value):
        raise AgentProtocolError("node_id must match spk_[0-9a-f]{32}")
    return value


def _deadline(value: Any) -> datetime:
    if isinstance(value, datetime):
        deadline = value
    elif isinstance(value, str):
        try:
            deadline = datetime.fromisoformat(value)
        except ValueError as error:
            raise AgentProtocolError("deadline must be an ISO-8601 UTC timestamp") from error
    else:
        raise AgentProtocolError("deadline must be an ISO-8601 UTC timestamp")
    if deadline.tzinfo is None or deadline.utcoffset() != UTC.utcoffset(deadline):
        raise AgentProtocolError("deadline must be aware UTC")
    return deadline.astimezone(UTC)


def _attempt_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _version(value["schema_version"]),
        "job_id": _uuid(value["job_id"], name="job_id"),
        "operation_id": _uuid(value["operation_id"], name="operation_id"),
        "attempt": _attempt(value["attempt"]),
        "fence": _uuid(value["fence"], name="fence"),
        "node_id": _node_id(value["node_id"]),
        "deadline": _deadline(value["deadline"]),
    }


@dataclass(frozen=True)
class AgentClaim:
    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    operation: AgentOperation
    base_commit: str
    payload_digest: str
    payload: Mapping[str, Any]
    deadline: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(self, "operation_id", _uuid(self.operation_id, name="operation_id"))
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        if not isinstance(self.operation, AgentOperation):
            raise AgentProtocolError("operation is not supported")
        if not isinstance(self.base_commit, str) or not COMMIT.fullmatch(self.base_commit):
            raise AgentProtocolError("base_commit must be a 40-character lowercase SHA-1")
        if not isinstance(self.payload_digest, str) or not DIGEST.fullmatch(self.payload_digest):
            raise AgentProtocolError("payload_digest must be a lowercase SHA-256")
        payload = _validate_bounded_document(self.payload, name="payload")
        if hashlib.sha256(canonical_message(payload)).hexdigest() != self.payload_digest:
            raise AgentProtocolError("payload digest does not match payload")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "deadline", _deadline(self.deadline))

    @classmethod
    def parse(cls, raw: Any) -> "AgentClaim":
        value = _mapping(raw)
        _fields(
            value,
            required={
                "schema_version", "job_id", "operation_id", "attempt", "fence", "node_id",
                "operation", "base_commit", "payload_digest", "payload", "deadline",
            },
        )
        try:
            operation = AgentOperation(value["operation"])
        except (TypeError, ValueError) as error:
            raise AgentProtocolError("operation is not supported") from error
        base_commit = value["base_commit"]
        if not isinstance(base_commit, str) or not COMMIT.fullmatch(base_commit):
            raise AgentProtocolError("base_commit must be a 40-character lowercase SHA-1")
        payload_digest = value["payload_digest"]
        if not isinstance(payload_digest, str) or not DIGEST.fullmatch(payload_digest):
            raise AgentProtocolError("payload_digest must be a lowercase SHA-256")
        return cls(**_attempt_fields(value), operation=operation, base_commit=base_commit, payload_digest=payload_digest, payload=value["payload"])


@dataclass(frozen=True)
class AgentProgress:
    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    deadline: datetime
    progress: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(self, "operation_id", _uuid(self.operation_id, name="operation_id"))
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        object.__setattr__(self, "deadline", _deadline(self.deadline))
        object.__setattr__(self, "progress", _validate_bounded_document(self.progress, name="progress"))

    @classmethod
    def parse(cls, raw: Any) -> "AgentProgress":
        value = _mapping(raw)
        _fields(value, required={"schema_version", "job_id", "operation_id", "attempt", "fence", "node_id", "deadline", "progress"})
        return cls(**_attempt_fields(value), progress=value["progress"])


@dataclass(frozen=True)
class AgentResult:
    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    deadline: datetime
    state: str
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(self, "operation_id", _uuid(self.operation_id, name="operation_id"))
        object.__setattr__(self, "attempt", _attempt(self.attempt))
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        object.__setattr__(self, "deadline", _deadline(self.deadline))
        if self.state not in {"succeeded", "failed", "waiting-for-operator"}:
            raise AgentProtocolError("result state is not supported")
        object.__setattr__(self, "result", _validate_bounded_document(self.result, name="result"))

    @classmethod
    def parse(cls, raw: Any) -> "AgentResult":
        value = _mapping(raw)
        _fields(value, required={"schema_version", "job_id", "operation_id", "attempt", "fence", "node_id", "deadline", "state", "result"})
        return cls(**_attempt_fields(value), state=value["state"], result=value["result"])
