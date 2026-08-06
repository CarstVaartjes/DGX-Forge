"""Generic workload package operations shared by control and Spark agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import (
    AgentOperation,
    AgentProtocolError,
    _attempt_fields,
    _deadline,
    _fields,
    _mapping,
    _node_id,
    _uuid,
    _version,
)

_DEPLOYMENT_ID = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TARGET_BYTES = 16 * 1024**4

RELEASE_BOUND_PACKAGE_OPERATIONS = frozenset(
    {
        AgentOperation.PACKAGE_PREPARE,
        AgentOperation.PACKAGE_ACTIVATE,
        AgentOperation.PACKAGE_HEALTH,
        AgentOperation.PACKAGE_STOP,
        AgentOperation.PACKAGE_ROLLBACK,
        AgentOperation.PACKAGE_REMOVE,
        AgentOperation.PACKAGE_REPAIR,
    }
)
PACKAGE_OPERATIONS = RELEASE_BOUND_PACKAGE_OPERATIONS | {
    AgentOperation.PACKAGE_GC
}


def _digest(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class PackageOperationRequest:
    """One closed, family-agnostic package operation request."""

    operation: AgentOperation
    schema_version: int
    deployment_id: str | None = None
    release_digest: str | None = None
    deployment_digest: str | None = None
    dry_run: bool | None = None
    target_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.operation not in PACKAGE_OPERATIONS:
            raise AgentProtocolError("package operation is not supported")
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        if self.operation in RELEASE_BOUND_PACKAGE_OPERATIONS:
            if (
                not isinstance(self.deployment_id, str)
                or _DEPLOYMENT_ID.fullmatch(self.deployment_id) is None
            ):
                raise AgentProtocolError("deployment_id is invalid")
            object.__setattr__(
                self,
                "release_digest",
                _digest(self.release_digest, name="release_digest"),
            )
            object.__setattr__(
                self,
                "deployment_digest",
                _digest(self.deployment_digest, name="deployment_digest"),
            )
            if self.dry_run is not None or self.target_bytes is not None:
                raise AgentProtocolError("package operation fields do not match operation")
            return
        if (
            self.deployment_id is not None
            or self.release_digest is not None
            or self.deployment_digest is not None
            or not isinstance(self.dry_run, bool)
        ):
            raise AgentProtocolError("package operation fields do not match operation")
        if self.target_bytes is not None and (
            not isinstance(self.target_bytes, int)
            or isinstance(self.target_bytes, bool)
            or not 1 <= self.target_bytes <= _MAX_TARGET_BYTES
        ):
            raise AgentProtocolError("target_bytes must be a bounded positive integer")

    @classmethod
    def parse(
        cls, operation: AgentOperation, payload: Any
    ) -> PackageOperationRequest:
        if not isinstance(operation, AgentOperation) or operation not in PACKAGE_OPERATIONS:
            raise AgentProtocolError("package operation is not supported")
        value = _mapping(payload)
        if operation in RELEASE_BOUND_PACKAGE_OPERATIONS:
            _fields(
                value,
                required={
                    "schema_version",
                    "deployment_id",
                    "release_digest",
                    "deployment_digest",
                },
            )
            return cls(
                operation=operation,
                schema_version=value["schema_version"],
                deployment_id=value["deployment_id"],
                release_digest=value["release_digest"],
                deployment_digest=value["deployment_digest"],
            )
        expected = {"schema_version", "dry_run"}
        if "target_bytes" in value:
            expected.add("target_bytes")
        _fields(value, required=expected)
        return cls(
            operation=operation,
            schema_version=value["schema_version"],
            dry_run=value["dry_run"],
            target_bytes=value.get("target_bytes"),
        )


@dataclass(frozen=True)
class AgentDirective:
    """Authenticated heartbeat response for deadline renewal and cancellation."""

    schema_version: int
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    node_id: str
    deadline: datetime
    cancel_requested: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _version(self.schema_version))
        object.__setattr__(self, "job_id", _uuid(self.job_id, name="job_id"))
        object.__setattr__(
            self, "operation_id", _uuid(self.operation_id, name="operation_id")
        )
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < 1
        ):
            raise AgentProtocolError("attempt must be a positive integer")
        object.__setattr__(self, "fence", _uuid(self.fence, name="fence"))
        object.__setattr__(self, "node_id", _node_id(self.node_id))
        object.__setattr__(self, "deadline", _deadline(self.deadline))
        if not isinstance(self.cancel_requested, bool):
            raise AgentProtocolError("cancel_requested must be a boolean")

    @classmethod
    def parse(cls, raw: Any) -> AgentDirective:
        value = _mapping(raw)
        _fields(
            value,
            required={
                "schema_version",
                "job_id",
                "operation_id",
                "attempt",
                "fence",
                "node_id",
                "deadline",
                "cancel_requested",
            },
        )
        return cls(
            **_attempt_fields(value),
            cancel_requested=value["cancel_requested"],
        )
