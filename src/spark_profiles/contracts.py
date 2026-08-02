"""Strict, immutable contracts for declarative Spark workload profiles."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import ValidationError, validate

_NODES = frozenset(("spark1", "spark2"))


class ProfileValidationError(ValueError):
    """Raised when a workload or cluster profile violates its contract."""


@dataclass(frozen=True)
class SourcePin:
    repository: str
    commit: str


@dataclass(frozen=True)
class CheckpointPin:
    repository: str
    revision: str
    manifest: Path
    manifest_sha256: str | None


@dataclass(frozen=True)
class ImagePin:
    reference: str


@dataclass(frozen=True)
class RuntimeRelease:
    manifest: Path
    sha256: str


@dataclass(frozen=True)
class OperationTimeouts:
    prepare: float
    verify: float
    start: float
    health: float
    infer: float
    stop: float
    verify_release: float

    def for_operation(self, operation: str) -> float:
        return getattr(self, operation.replace("-", "_"))


@dataclass(frozen=True)
class WorkloadPaths:
    cache: Path
    scratch: Path
    output: Path


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


@dataclass(frozen=True)
class AdapterCommands:
    prepare: tuple[str, ...]
    verify: tuple[str, ...]
    start: tuple[str, ...]
    health: tuple[str, ...]
    infer: tuple[str, ...]
    stop: tuple[str, ...]
    verify_release: tuple[str, ...]


@dataclass(frozen=True)
class ResourceEnvelope:
    minimum_free_memory_bytes: int
    minimum_free_disk_bytes: int
    stop_memory_tolerance_bytes: int


@dataclass(frozen=True)
class WorkloadDefinition:
    id: str
    adapter: str
    topology: str
    placement_class: str
    nodes: tuple[str, ...]
    start_order: tuple[str, ...]
    stop_order: tuple[str, ...]
    conflicts: tuple[str, ...]
    co_location: str
    accepted_evidence: Path
    source: SourcePin
    checkpoint: CheckpointPin
    image: ImagePin
    paths: WorkloadPaths
    endpoint: Endpoint
    commands: AdapterCommands
    resources: ResourceEnvelope
    runtime_release: RuntimeRelease | None = field(
        default=None, metadata={"omit_if_none": True}
    )
    deadlines: OperationTimeouts | None = field(
        default=None, metadata={"omit_if_none": True}
    )


@dataclass(frozen=True)
class ClusterProfile:
    id: str
    accepted_evidence: Path
    placements: Mapping[str, tuple[str, ...]]
    endpoints: Mapping[str, str]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as profile_file:
            return tomllib.load(profile_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ProfileValidationError(f"cannot load profile {path}: {error}") from error


def _load_schema(name: str) -> dict[str, Any]:
    try:
        schema = resources.files("spark_profiles").joinpath("schemas", name)
        with schema.open(encoding="utf-8") as schema_file:
            import json

            return json.load(schema_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"cannot load contract schema {name}: {error}") from error


def _validate(data: dict[str, Any], schema_name: str) -> None:
    try:
        validate(instance=data, schema=_load_schema(schema_name))
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path)
        prefix = f"{location}: " if location else ""
        raise ProfileValidationError(f"{prefix}{error.message}") from error


def _require_rank_orders(data: dict[str, Any]) -> None:
    nodes = tuple(data["nodes"])
    start_order = tuple(data["start_order"])
    stop_order = tuple(data["stop_order"])
    if len(set(nodes)) != len(nodes) or set(nodes) - _NODES:
        raise ProfileValidationError("nodes must contain unique Spark node IDs")
    if set(start_order) != set(nodes) or len(start_order) != len(nodes):
        raise ProfileValidationError("start_order must rank every declared node exactly once")
    if set(stop_order) != set(nodes) or len(stop_order) != len(nodes):
        raise ProfileValidationError("stop_order must rank every declared node exactly once")
    if data["topology"] == "distributed" and set(nodes) != _NODES:
        raise ProfileValidationError("distributed workloads require spark1 and spark2")
    if data["topology"] == "distributed" and start_order != ("spark2", "spark1"):
        raise ProfileValidationError("distributed workloads require worker-first start order")
    if data["topology"] == "distributed" and stop_order != ("spark1", "spark2"):
        raise ProfileValidationError("distributed workloads require head-first stop order")
    if data["topology"] == "single" and len(nodes) != 1:
        raise ProfileValidationError("single workloads require exactly one node")


def _command(value: list[str]) -> tuple[str, ...]:
    return tuple(value)


def load_workload(path: Path) -> WorkloadDefinition:
    """Load and strictly validate a declarative workload definition."""
    data = _read_toml(path)
    _validate(data, "workload.schema.json")
    _require_rank_orders(data)
    endpoint = data["endpoint"]
    if endpoint["host"] not in {"127.0.0.1", "::1"}:
        raise ProfileValidationError("endpoint host must be loopback-only")
    commands = data["commands"]
    return WorkloadDefinition(
        id=data["id"],
        adapter=data["adapter"],
        topology=data["topology"],
        placement_class=data["placement_class"],
        nodes=tuple(data["nodes"]),
        start_order=tuple(data["start_order"]),
        stop_order=tuple(data["stop_order"]),
        conflicts=tuple(data["conflicts"]),
        co_location=data["co_location"],
        accepted_evidence=Path(data["accepted_evidence"]),
        source=SourcePin(**data["source"]),
        checkpoint=CheckpointPin(
            repository=data["checkpoint"]["repository"],
            revision=data["checkpoint"]["revision"],
            manifest=Path(data["checkpoint"]["manifest"]),
            manifest_sha256=data["checkpoint"].get("manifest_sha256"),
        ),
        image=ImagePin(reference=data["image"]["reference"]),
        runtime_release=(
            RuntimeRelease(
                manifest=Path(data["runtime_release"]["manifest"]),
                sha256=data["runtime_release"]["sha256"],
            )
            if "runtime_release" in data
            else None
        ),
        paths=WorkloadPaths(
            cache=Path(data["paths"]["cache"]),
            scratch=Path(data["paths"]["scratch"]),
            output=Path(data["paths"]["output"]),
        ),
        endpoint=Endpoint(**endpoint),
        commands=AdapterCommands(
            prepare=_command(commands["prepare"]),
            verify=_command(commands["verify"]),
            start=_command(commands["start"]),
            health=_command(commands["health"]),
            infer=_command(commands["infer"]),
            stop=_command(commands["stop"]),
            verify_release=_command(commands["verify-release"]),
        ),
        resources=ResourceEnvelope(**data["resources"]),
        deadlines=(
            OperationTimeouts(
                prepare=data["deadlines"]["prepare"],
                verify=data["deadlines"]["verify"],
                start=data["deadlines"]["start"],
                health=data["deadlines"]["health"],
                infer=data["deadlines"]["infer"],
                stop=data["deadlines"]["stop"],
                verify_release=data["deadlines"]["verify-release"],
            )
            if "deadlines" in data
            else None
        ),
    )


def load_cluster_profile(path: Path) -> ClusterProfile:
    """Load and strictly validate a whole-cluster profile."""
    data = _read_toml(path)
    _validate(data, "cluster-profile.schema.json")
    placements = {
        node: tuple(workloads) for node, workloads in data["placements"].items()
    }
    return ClusterProfile(
        id=data["id"],
        accepted_evidence=Path(data["accepted_evidence"]),
        placements=MappingProxyType(placements),
        endpoints=MappingProxyType(dict(data["endpoints"])),
    )
