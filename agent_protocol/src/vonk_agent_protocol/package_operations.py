"""Generic workload package operations shared by control and Spark agents."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
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
    canonical_message,
)

_DEPLOYMENT_ID = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TARGET_BYTES = 16 * 1024**4
_MAX_DEPLOYMENT_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_PORT = range(1024, 65536)

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


def _parse_deployment(
    value: object,
    *,
    deployment_id: str,
    release_digest: str,
    deployment_config_digest: str,
) -> Mapping[str, object]:
    """Validate the immutable deployment projection carried to a Spark.

    The control plane sends the repository-authored deployment alongside its
    digest.  This keeps the package operation ABI generic while ensuring the
    agent never invents execution policy from a model catalog or local
    defaults.  Secret values are intentionally only references; they are
    retained for digest binding and are never copied into an invocation.
    """
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AgentProtocolError("deployment must be an object")
    required = {
        "schema_version", "deployment_id", "family_id", "release_digest",
        "selector", "secrets", "ports", "arguments", "routing", "resources",
    }
    optional = {"mounts", "devices", "network"}
    if set(value) - required - optional or not required <= set(value):
        raise AgentProtocolError("deployment fields are invalid")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise AgentProtocolError("deployment schema_version is invalid")
    if value["deployment_id"] != deployment_id or not isinstance(value["deployment_id"], str) or _DEPLOYMENT_ID.fullmatch(value["deployment_id"]) is None:
        raise AgentProtocolError("deployment identity does not match operation")
    if not isinstance(value["release_digest"], str) or _DIGEST.fullmatch(value["release_digest"]) is None:
        raise AgentProtocolError("deployment release identity is invalid")
    if not isinstance(value["family_id"], str) or _IDENTIFIER.fullmatch(value["family_id"]) is None:
        raise AgentProtocolError("deployment family_id is invalid")
    selector = value["selector"]
    if not isinstance(selector, Mapping):
        raise AgentProtocolError("deployment selector is invalid")
    arguments = value["arguments"]
    if not isinstance(arguments, (list, tuple)) or len(arguments) > 128 or any(
        not isinstance(item, str) or len(item) > 4096 or any(ord(char) < 0x20 or ord(char) == 0x7f for char in item)
        for item in arguments
    ):
        raise AgentProtocolError("deployment arguments are invalid")
    ports = value["ports"]
    if not isinstance(ports, Mapping) or len(ports) > 32 or any(
        not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None or item not in _PORT
        for key, item in ports.items()
    ):
        raise AgentProtocolError("deployment ports are invalid")
    routing = value["routing"]
    if not isinstance(routing, Mapping) or set(routing) != {"alias", "port"} or routing.get("port") not in ports:
        raise AgentProtocolError("deployment routing is invalid")
    resources = value["resources"]
    if not isinstance(resources, Mapping) or not {"memory_bytes", "storage_bytes", "gpu_count"} <= set(resources):
        raise AgentProtocolError("deployment resources are invalid")
    if any(
        isinstance(resources.get(key), bool) or not isinstance(resources.get(key), int) or resources.get(key) < 0
        for key in ("memory_bytes", "storage_bytes", "gpu_count")
    ):
        raise AgentProtocolError("deployment resources are invalid")
    secrets = value["secrets"]
    if not isinstance(secrets, Mapping) or len(secrets) > 32 or any(
        not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None
        or not isinstance(item, str) or not item.startswith("secret://")
        for key, item in secrets.items()
    ):
        raise AgentProtocolError("deployment secret references are invalid")
    # Keep optional execution policy typed and bounded.  Mount objects refer
    # only to signed package objects; no host paths can cross this boundary.
    mounts = value.get("mounts", ())
    if not isinstance(mounts, (list, tuple)) or len(mounts) > 64 or any(
        not isinstance(item, Mapping)
        or set(item) != {"object_digest", "target", "read_only"}
        or not isinstance(item.get("object_digest"), str)
        or _DIGEST.fullmatch(item["object_digest"]) is None
        or not isinstance(item.get("target"), str)
        or not item["target"]
        or item.get("read_only") is not True
        for item in mounts
    ):
        raise AgentProtocolError("deployment mounts are invalid")
    devices = value.get("devices", ())
    if not isinstance(devices, (list, tuple)) or len(devices) > 32 or any(
        not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None for item in devices
    ):
        raise AgentProtocolError("deployment devices are invalid")
    network = value.get("network", {"mode": "none", "egress": []})
    if not isinstance(network, Mapping) or set(network) != {"mode", "egress"} or network.get("mode") not in {"none", "restricted"} or not isinstance(network.get("egress"), (list, tuple)):
        raise AgentProtocolError("deployment network policy is invalid")
    try:
        frozen = {key: tuple(item) if key in {"arguments", "mounts", "devices"} and isinstance(item, list) else item for key, item in value.items()}
        # WorkloadDeployment canonical bytes include the repository contract's
        # terminal newline; bind the same bytes used by control's digest.
        raw = canonical_message(frozen) + b"\n"
    except Exception as error:
        raise AgentProtocolError("deployment is not canonical") from error
    if len(raw) > _MAX_DEPLOYMENT_BYTES:
        raise AgentProtocolError("deployment is too large")
    if hashlib.sha256(raw).hexdigest() != deployment_config_digest:
        raise AgentProtocolError("deployment digest does not match operation")
    return frozen


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
    deployment: Mapping[str, object] | None = None
    deployment_config_digest: str | None = None

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
            if self.deployment is not None:
                config_digest = _digest(
                    self.deployment_config_digest,
                    name="deployment_config_digest",
                )
                parsed = _parse_deployment(
                    self.deployment,
                    deployment_id=self.deployment_id,
                    release_digest=self.release_digest,
                    deployment_config_digest=config_digest,
                )
                object.__setattr__(self, "deployment", parsed)
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
            required = {
                "schema_version",
                "deployment_id",
                "release_digest",
                "deployment_digest",
            }
            if "deployment" in value:
                required.add("deployment")
                required.add("deployment_config_digest")
            _fields(value, required=required)
            return cls(
                operation=operation,
                schema_version=value["schema_version"],
                deployment_id=value["deployment_id"],
                release_digest=value["release_digest"],
                deployment_digest=value["deployment_digest"],
                deployment=value.get("deployment"),
                deployment_config_digest=value.get("deployment_config_digest"),
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
