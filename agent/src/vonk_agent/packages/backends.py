"""Closed, versioned invocation structs for workload execution backends."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from vonk_agent_protocol import OciBundleMetadata

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_DEVICE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_EGRESS = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:]+\]):(?:[1-9][0-9]{0,4})\Z"
)

MAX_ARGUMENTS = 32
MAX_MOUNTS = 64
MAX_DEVICES = 32


class BackendValidationError(ValueError):
    """A release-provided backend struct is not in the compiled vocabulary."""


class Backend(StrEnum):
    OCI = "oci"
    PYTHON_VENV = "python-venv"
    NATIVE = "native"


@dataclass(frozen=True)
class ResourcePolicy:
    cpu_millis: int
    memory_bytes: int
    pids_limit: int
    timeout_seconds: int
    output_limit_bytes: int

    def __post_init__(self) -> None:
        _integer(self.cpu_millis, "CPU", 1, 1_000_000)
        _integer(self.memory_bytes, "memory", 1, 2**60)
        _integer(self.pids_limit, "PID", 1, 65_536)
        _integer(self.timeout_seconds, "timeout", 1, 86_400)
        _integer(self.output_limit_bytes, "output", 1, 1024 * 1024)

    @classmethod
    def parse(cls, value: object) -> ResourcePolicy:
        document = _object(
            value,
            {
                "cpu_millis",
                "memory_bytes",
                "pids_limit",
                "timeout_seconds",
                "output_limit_bytes",
            },
            "resource fields",
        )
        return cls(
            document["cpu_millis"],
            document["memory_bytes"],
            document["pids_limit"],
            document["timeout_seconds"],
            document["output_limit_bytes"],
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "timeout_seconds": self.timeout_seconds,
            "output_limit_bytes": self.output_limit_bytes,
        }


@dataclass(frozen=True)
class MountPolicy:
    object_digest: str
    target: str
    read_only: bool = True

    def __post_init__(self) -> None:
        _digest(self.object_digest, "mount object digest")
        _relative_path(self.target, "mount target")
        if self.read_only is not True:
            raise BackendValidationError("package mounts must be read-only")

    @classmethod
    def parse(cls, value: object) -> MountPolicy:
        document = _object(
            value, {"object_digest", "target", "read_only"}, "mount fields"
        )
        return cls(document["object_digest"], document["target"], document["read_only"])

    def to_mapping(self) -> dict[str, object]:
        return {
            "object_digest": self.object_digest,
            "target": self.target,
            "read_only": True,
        }


@dataclass(frozen=True)
class NetworkPolicy:
    mode: str
    egress: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"none", "restricted"}:
            raise BackendValidationError("network mode is invalid")
        if len(self.egress) > 64 or len(set(self.egress)) != len(self.egress):
            raise BackendValidationError("network egress is invalid")
        if self.mode == "none" and self.egress:
            raise BackendValidationError(
                "network-disabled backends cannot declare egress"
            )
        if self.mode == "restricted" and not self.egress:
            raise BackendValidationError("restricted network requires bounded egress")
        for endpoint in self.egress:
            if not isinstance(endpoint, str) or not _EGRESS.fullmatch(endpoint):
                raise BackendValidationError("network egress endpoint is invalid")
            port = int(endpoint.rsplit(":", 1)[1])
            if port > 65_535:
                raise BackendValidationError("network egress port is invalid")

    @classmethod
    def parse(cls, value: object) -> NetworkPolicy:
        document = _object(value, {"mode", "egress"}, "network fields")
        return cls(document["mode"], _strings(document["egress"], "network egress", 64))

    def to_mapping(self) -> dict[str, object]:
        return {"mode": self.mode, "egress": list(self.egress)}


@dataclass(frozen=True)
class PythonRuntimePolicy:
    """Signed, generation-local Python interpreter selection."""

    environment_component: str
    environment_digest: str
    environment_tree_digest: str
    interpreter_component: str
    interpreter_component_digest: str
    interpreter_entrypoint: str
    interpreter_digest: str

    def __post_init__(self) -> None:
        _identifier(self.environment_component, "Python environment component")
        _digest(self.environment_digest, "Python environment digest")
        _digest(self.environment_tree_digest, "Python environment tree digest")
        _identifier(self.interpreter_component, "Python interpreter component")
        _digest(
            self.interpreter_component_digest,
            "Python interpreter component digest",
        )
        if self.environment_component == self.interpreter_component:
            raise BackendValidationError(
                "Python environment and interpreter components must differ"
            )
        _relative_path(
            self.interpreter_entrypoint, "Python interpreter entrypoint"
        )
        if PurePosixPath(self.interpreter_entrypoint).name in {
            "apt",
            "apt-get",
            "bash",
            "dash",
            "sh",
            "sudo",
        }:
            raise BackendValidationError("Python interpreter entrypoint is forbidden")
        _digest(self.interpreter_digest, "Python interpreter digest")

    @classmethod
    def parse(cls, value: object) -> PythonRuntimePolicy:
        document = _object(
            value,
            {
                "environment_component",
                "environment_digest",
                "environment_tree_digest",
                "interpreter_component",
                "interpreter_component_digest",
                "interpreter_entrypoint",
                "interpreter_digest",
            },
            "Python runtime fields",
        )
        return cls(
            document["environment_component"],
            document["environment_digest"],
            document["environment_tree_digest"],
            document["interpreter_component"],
            document["interpreter_component_digest"],
            document["interpreter_entrypoint"],
            document["interpreter_digest"],
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "environment_component": self.environment_component,
            "environment_digest": self.environment_digest,
            "environment_tree_digest": self.environment_tree_digest,
            "interpreter_component": self.interpreter_component,
            "interpreter_component_digest": self.interpreter_component_digest,
            "interpreter_entrypoint": self.interpreter_entrypoint,
            "interpreter_digest": self.interpreter_digest,
        }


@dataclass(frozen=True)
class BackendInvocation:
    schema_version: int
    backend: Backend
    release_digest: str
    generation: str
    entrypoint: str
    arguments: tuple[str, ...]
    resources: ResourcePolicy
    mounts: tuple[MountPolicy, ...]
    devices: tuple[str, ...]
    network: NetworkPolicy
    python_runtime: PythonRuntimePolicy | None = None
    oci_bundle: OciBundleMetadata | None = None
    oci_bundle_digest: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise BackendValidationError("backend schema version is invalid")
        if type(self.backend) is not Backend:
            raise BackendValidationError("backend is invalid")
        _digest(self.release_digest, "release digest")
        _identifier(self.generation, "generation")
        _relative_path(self.entrypoint, "entrypoint")
        if PurePosixPath(self.entrypoint).name in {
            "apt",
            "apt-get",
            "bash",
            "dash",
            "sh",
            "sudo",
        }:
            raise BackendValidationError("entrypoint requests a forbidden host tool")
        _argument_values(self.arguments)
        if type(self.resources) is not ResourcePolicy:
            raise BackendValidationError("resource policy is invalid")
        if (
            len(self.mounts) > MAX_MOUNTS
            or not all(type(value) is MountPolicy for value in self.mounts)
            or len({value.target for value in self.mounts}) != len(self.mounts)
        ):
            raise BackendValidationError("mounts are invalid")
        if (
            len(self.devices) > MAX_DEVICES
            or len(set(self.devices)) != len(self.devices)
            or any(
                not isinstance(value, str) or not _DEVICE.fullmatch(value)
                for value in self.devices
            )
        ):
            raise BackendValidationError("devices are invalid")
        if type(self.network) is not NetworkPolicy:
            raise BackendValidationError("network policy is invalid")
        if self.backend is Backend.PYTHON_VENV:
            if type(self.python_runtime) is not PythonRuntimePolicy:
                raise BackendValidationError(
                    "Python runtime metadata is required for python-venv"
                )
        elif self.python_runtime is not None:
            raise BackendValidationError(
                "Python runtime metadata belongs only to python-venv"
            )
        if self.oci_bundle is not None and type(self.oci_bundle) is not OciBundleMetadata:
            raise BackendValidationError("OCI bundle metadata is invalid")
        if self.oci_bundle is not None and self.backend is not Backend.OCI:
            raise BackendValidationError("OCI bundle metadata belongs only to OCI")
        if self.backend is Backend.OCI:
            if self.oci_bundle is None or self.oci_bundle_digest is None:
                raise BackendValidationError("OCI bundle digest is required")
            _digest(self.oci_bundle_digest, "OCI bundle digest")
        elif self.oci_bundle_digest is not None:
            raise BackendValidationError("OCI bundle digest belongs only to OCI")

    @classmethod
    def parse(cls, value: object) -> BackendInvocation:
        if not isinstance(value, Mapping):
            raise BackendValidationError("backend invocation fields are invalid")
        fields = {
            "schema_version",
            "backend",
            "release_digest",
            "generation",
            "entrypoint",
            "arguments",
            "resources",
            "mounts",
            "devices",
            "network",
        }
        allowed = (
            fields,
            fields | {"python_runtime"},
            fields | {"oci_bundle", "oci_bundle_digest"},
            fields | {"python_runtime", "oci_bundle", "oci_bundle_digest"},
        )
        if set(value) not in allowed:
            raise BackendValidationError("backend invocation fields are invalid")
        document = value
        try:
            backend = Backend(document["backend"])
        except (TypeError, ValueError) as error:
            raise BackendValidationError("backend is invalid") from error
        mounts_raw = _sequence(document["mounts"], "mounts", MAX_MOUNTS)
        devices = _strings(document["devices"], "devices", MAX_DEVICES)
        return cls(
            document["schema_version"],
            backend,
            document["release_digest"],
            document["generation"],
            document["entrypoint"],
            _arguments(document["arguments"]),
            ResourcePolicy.parse(document["resources"]),
            tuple(MountPolicy.parse(item) for item in mounts_raw),
            devices,
            NetworkPolicy.parse(document["network"]),
            None
            if document.get("python_runtime") is None
            else PythonRuntimePolicy.parse(document["python_runtime"]),
            None
            if document.get("oci_bundle") is None
            else OciBundleMetadata.parse(document["oci_bundle"]),
            None
            if document.get("oci_bundle_digest") is None
            else document["oci_bundle_digest"],
        )

    def to_mapping(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": 1,
            "backend": self.backend.value,
            "release_digest": self.release_digest,
            "generation": self.generation,
            "entrypoint": self.entrypoint,
            "arguments": list(self.arguments),
            "resources": self.resources.to_mapping(),
            "mounts": [mount.to_mapping() for mount in self.mounts],
            "devices": list(self.devices),
            "network": self.network.to_mapping(),
        }
        if self.python_runtime is not None:
            document["python_runtime"] = self.python_runtime.to_mapping()
        if self.oci_bundle is not None:
            document["oci_bundle"] = self.oci_bundle.to_mapping()
            document["oci_bundle_digest"] = self.oci_bundle_digest
        return document


def _object(value: object, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise BackendValidationError(f"{name} are invalid")
    if set(value) != fields:
        raise BackendValidationError(f"{name} are invalid")
    return value


def _sequence(value: object, name: str, maximum: int) -> Sequence[object]:
    if (
        not isinstance(value, (list, tuple))
        or isinstance(value, (str, bytes))
        or len(value) > maximum
    ):
        raise BackendValidationError(f"{name} are invalid")
    return value


def _strings(value: object, name: str, maximum: int) -> tuple[str, ...]:
    values = _sequence(value, name, maximum)
    if not all(isinstance(item, str) for item in values):
        raise BackendValidationError(f"{name} are invalid")
    return tuple(values)


def _arguments(value: object) -> tuple[str, ...]:
    values = _strings(value, "arguments", MAX_ARGUMENTS)
    _argument_values(values)
    return values


def _argument_values(values: tuple[str, ...]) -> None:
    if len(values) > MAX_ARGUMENTS:
        raise BackendValidationError("arguments are invalid")
    for value in values:
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > 1024
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in value
            )
            or value.startswith("/")
            or ".." in PurePosixPath(value).parts
        ):
            raise BackendValidationError("arguments are invalid")


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise BackendValidationError(f"{name} limit is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise BackendValidationError(f"{name} is invalid")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise BackendValidationError(f"{name} is invalid")
    return value


def _relative_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or "\\" in value:
        raise BackendValidationError(f"{name} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BackendValidationError(f"{name} is invalid")
    return value
