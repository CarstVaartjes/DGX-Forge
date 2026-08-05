"""Repository-pinned desired state resolved into deterministic agent work."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from dgx_agent_protocol import AgentOperation
from jsonschema import ValidationError, validate
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from spark_profiles.fleet import Fleet, ManagementEndpoint, NodeId, NodeRecord
from spark_profiles.placement import (
    NodeObservation,
    PlacementPlanner,
    PlacementRequirement,
)

from .models import AgentNode, Observation
from .orchestration import OperationGraph, OperationNode

if TYPE_CHECKING:
    from .reconcile import ReconciliationPlan
    from .repository import RepositoryService, TypedDocument

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}\Z")
_SUPPORTED_PROTOCOL_RANGE = (1, 1)
_REQUIRED_CAPABILITIES = frozenset(
    {
        AgentOperation.RELEASE_INSTALL.value,
        AgentOperation.WORKLOAD_PREPARE.value,
        AgentOperation.WORKLOAD_START.value,
        AgentOperation.WORKLOAD_STOP.value,
        AgentOperation.WORKLOAD_HEALTH.value,
        AgentOperation.WORKLOAD_VERIFY.value,
    }
)
_IMPLEMENTED_CAPABILITIES = _REQUIRED_CAPABILITIES | {
    AgentOperation.NODE_PROBE.value
}
_PLANNED_OPERATIONS = (
    AgentOperation.RELEASE_INSTALL.value,
    AgentOperation.WORKLOAD_PREPARE.value,
    AgentOperation.WORKLOAD_START.value,
    AgentOperation.WORKLOAD_HEALTH.value,
    AgentOperation.WORKLOAD_VERIFY.value,
)


@dataclass(frozen=True)
class DesiredStateObservation:
    """Durable capacity and connected-agent evidence for one fleet node."""

    node_id: str
    observed_at: datetime
    healthy: bool
    memory_available_bytes: int
    disk_available_bytes: int
    occupied: bool
    agent_state: str
    protocol_version: int | None
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        NodeId.parse(self.node_id)
        if not isinstance(self.observed_at, datetime):
            raise TypeError("observation timestamp is invalid")
        if not isinstance(self.healthy, bool) or not isinstance(self.occupied, bool):
            raise TypeError("observation health state is invalid")
        if (
            not isinstance(self.memory_available_bytes, int)
            or isinstance(self.memory_available_bytes, bool)
            or self.memory_available_bytes < 0
            or not isinstance(self.disk_available_bytes, int)
            or isinstance(self.disk_available_bytes, bool)
            or self.disk_available_bytes < 0
        ):
            raise ValueError("observation capacity is invalid")
        capabilities = tuple(self.capabilities)
        if len(capabilities) != len(set(capabilities)) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ValueError("agent capabilities are invalid")
        object.__setattr__(self, "capabilities", capabilities)


class DesiredStateResolver:
    """Resolve V2 repository definitions without static-plan fallback."""

    def __init__(
        self,
        repository: RepositoryService,
        *,
        clock: Callable[[], datetime],
        observation_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if observation_ttl <= timedelta(0):
            raise ValueError("observation TTL must be positive")
        self._repository = repository
        self._clock = clock
        self._observation_ttl = observation_ttl

    def resolve(
        self,
        commit: str,
        profile_id: str,
        observations: Iterable[DesiredStateObservation],
    ) -> ReconciliationPlan:
        if not isinstance(profile_id, str) or _IDENTIFIER.fullmatch(profile_id) is None:
            raise ValueError("profile ID is invalid")
        documents: dict[str, TypedDocument] = {}

        def read(path: str) -> TypedDocument:
            document = self._repository.read_document(commit, path)
            documents[path] = document
            return document

        fleet_document = read("inventory/fleet.toml")
        topology_document = read("inventory/topology.json")
        profile_document = read(f"config/cluster-profiles/{profile_id}.toml")
        fleet_data = _mapping(fleet_document.parsed, "fleet")
        topology = _mapping(topology_document.parsed, "topology")
        profile = _mapping(profile_document.parsed, "cluster profile")
        _schema(fleet_data, "fleet.schema.json", "fleet")
        _schema(topology, "topology.schema.json", "topology")
        _schema(profile, "cluster-profile-v2.schema.json", "cluster profile")
        if profile.get("id") != profile_id:
            raise ValueError("cluster profile ID does not match its path")
        fleet = _fleet(fleet_data)
        topology = _canonical_topology(topology)

        evidence_path = _repository_path(
            profile["accepted_evidence"], prefix="inventory/reports/"
        )
        evidence = _mapping(read(evidence_path).parsed, "accepted evidence")
        if set(evidence) != {"accepted", "schema_version"} or evidence != {
            "accepted": True,
            "schema_version": 1,
        }:
            raise ValueError("profile accepted evidence is invalid")

        workload_ids = tuple(sorted(cast(Sequence[str], profile["workloads"])))
        if not all(_IDENTIFIER.fullmatch(item) for item in workload_ids):
            raise ValueError("profile workload reference is invalid")
        requirements = _requirements(profile, workload_ids)
        workloads: dict[str, Mapping[str, Any]] = {}
        releases: dict[str, Mapping[str, Any]] = {}
        for workload_id in workload_ids:
            workload_document = read(f"config/workloads/{workload_id}.toml")
            workload = _mapping(workload_document.parsed, "workload")
            _schema(workload, "workload-v2.schema.json", "workload")
            if workload.get("id") != workload_id:
                raise ValueError("profile workload reference does not match its path")
            workload_hash = cast(str, workload["definition_hash"])
            if requirements[workload_id].definition_hash != workload_hash:
                raise ValueError("profile definition hash does not match workload")
            release_path = f"manifests/releases/{workload_id}.json"
            release_document = read(release_path)
            release = _release(
                _mapping(release_document.parsed, "release manifest"),
                workload_id=workload_id,
                definition_hash=workload_hash,
            )
            workloads[workload_id] = workload
            releases[workload_id] = MappingProxyType(
                {
                    "manifest_path": release_path,
                    "manifest_sha256": release_document.sha256,
                    "definition_hash": workload_hash,
                    "artifact": release["artifact"],
                    "endpoint": release["endpoint"],
                }
            )
        _profile_cross_references(profile, workloads)

        placement_observations = _placement_observations(
            observations,
            fleet=fleet,
            now=self._clock(),
            ttl=self._observation_ttl,
        )
        placements: dict[str, tuple[str, ...]] = {}
        placement_inputs: dict[str, str] = {}
        available = placement_observations
        planner = PlacementPlanner()
        for workload_id in workload_ids:
            requirement = requirements[workload_id]
            placement = planner.plan(requirement, fleet, topology, available.values())
            placements[workload_id] = tuple(node.value for node in placement.nodes)
            placement_inputs[workload_id] = placement.input_digest
            if requirement.exclusive:
                available = dict(available)
                for node_id in placement.nodes:
                    available[node_id] = replace(available[node_id], occupied=True)

        routes = _routes(profile, placements, releases)
        operation_graph, payloads = _operations(
            commit,
            placements=placements,
            releases=releases,
            placement_inputs=placement_inputs,
            lifecycle=_mapping(profile["lifecycle"], "profile lifecycle"),
        )
        input_digests = {
            path: document.sha256 for path, document in sorted(documents.items())
        }
        from .reconcile import resolved_reconciliation_plan

        return resolved_reconciliation_plan(
            commit=commit,
            targets=operation_graph.targets,
            placements=placements,
            routes=routes,
            releases=releases,
            input_digests=input_digests,
            operation_graph=operation_graph,
            operation_payloads=payloads,
            agent_protocol_range=_SUPPORTED_PROTOCOL_RANGE,
        )


def durable_desired_state_observations(
    sessions: sessionmaker[Session],
) -> tuple[DesiredStateObservation, ...]:
    """Project latest durable health and agent state into resolver evidence."""

    with sessions() as session:
        agents = tuple(
            session.scalars(select(AgentNode).order_by(AgentNode.node_id))
        )
        health = tuple(
            session.scalars(
                select(Observation)
                .where(Observation.kind == "health")
                .order_by(Observation.observed_at.desc(), Observation.id)
            )
        )
    latest: dict[str, Observation] = {}
    for observation in health:
        latest.setdefault(observation.node_id, observation)
    projected: list[DesiredStateObservation] = []
    for agent in agents:
        observation = latest.get(agent.node_id)
        if observation is None:
            continue
        payload = _mapping(observation.payload, "health observation")
        memory = payload.get("memory_available_bytes")
        disk = payload.get("disk_available_bytes")
        occupied = payload.get("occupied")
        if (
            not isinstance(memory, int)
            or isinstance(memory, bool)
            or not isinstance(disk, int)
            or isinstance(disk, bool)
            or not isinstance(occupied, bool)
        ):
            raise TypeError("health observation capacity is invalid")
        observed_at = _aware(observation.observed_at)
        state = agent.state
        if agent.last_seen_at is None:
            state = "offline"
        else:
            observed_at = min(observed_at, _aware(agent.last_seen_at))
        projected.append(
            DesiredStateObservation(
                node_id=agent.node_id,
                observed_at=observed_at,
                healthy=payload.get("status") in {"healthy", "warning"},
                memory_available_bytes=memory,
                disk_available_bytes=disk,
                occupied=occupied,
                agent_state=state,
                protocol_version=agent.protocol_version,
                capabilities=tuple(sorted(agent.capabilities)),
            )
        )
    return tuple(projected)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _schema(document: Mapping[str, Any], name: str, field: str) -> None:
    try:
        with resources.files("spark_profiles.schemas").joinpath(name).open(
            encoding="utf-8"
        ) as source:
            schema = json.load(source)
        validate(instance=document, schema=schema)
    except ValidationError as error:
        raise ValueError(f"{field} schema is invalid") from error


def _fleet(document: Mapping[str, Any]) -> Fleet:
    records: dict[NodeId, NodeRecord] = {}
    for node_id_text, raw_node in sorted(_mapping(document["nodes"], "fleet nodes").items()):
        node = _mapping(raw_node, "fleet node")
        management = _mapping(node["management"], "fleet management")
        node_id = NodeId.parse(node_id_text)
        records[node_id] = NodeRecord(
            node_id,
            cast(str, node["display_name"]),
            cast(str, node["hostname"]),
            ManagementEndpoint(
                cast(str, management["host"]),
                cast(str, management["user"]),
                cast(int, management["port"]),
                cast(str | None, management.get("credential_ref")),
            ),
            dict(_mapping(node["labels"], "fleet labels")),
            cast(Any, node["lifecycle"]),
        )
    return Fleet(2, records)


def _canonical_topology(document: Mapping[str, Any]) -> Mapping[str, object]:
    links = []
    for raw in cast(Sequence[Mapping[str, Any]], document["links"]):
        link = dict(raw)
        link["endpoints"] = sorted(
            (dict(item) for item in cast(Sequence[Mapping[str, object]], raw["endpoints"])),
            key=lambda item: (str(item["node_id"]), str(item["interface"])),
        )
        links.append(link)
    return {
        "schema_version": 1,
        "nodes": sorted(cast(Sequence[str], document["nodes"])),
        "links": sorted(links, key=lambda item: str(item["id"])),
    }


def _requirements(
    profile: Mapping[str, Any], workload_ids: tuple[str, ...]
) -> Mapping[str, PlacementRequirement]:
    requirements: dict[str, PlacementRequirement] = {}
    for raw in cast(Sequence[Mapping[str, Any]], profile["requirements"]):
        workload_id = cast(str, raw["workload"])
        if workload_id in requirements:
            raise ValueError("profile workload requirement is duplicate")
        requirements[workload_id] = PlacementRequirement(
            name=workload_id,
            definition_hash=cast(str, raw["definition_hash"]),
            node_count=cast(int, raw["node_count"]),
            required_labels=dict(
                _mapping(raw["required_labels"], "required labels")
            ),
            min_memory_bytes=cast(int, raw["min_memory_bytes"]),
            min_disk_bytes=cast(int, raw["min_disk_bytes"]),
            exclusive=cast(bool, raw["exclusive"]),
            distributed=cast(int, raw["node_count"]) > 1,
            model_supports_distributed=cast(bool, raw["distributed_supported"]),
            preferred_node_ids=tuple(cast(Sequence[str], raw.get("preferred_node_ids", ()))),
        )
    if set(requirements) != set(workload_ids):
        raise ValueError("profile workload reference lacks one exact requirement")
    return MappingProxyType(requirements)


def _repository_path(value: object, *, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or ".." in value:
        raise ValueError("repository reference is invalid")
    return value


def _release(
    document: Mapping[str, Any], *, workload_id: str, definition_hash: str
) -> Mapping[str, Any]:
    if set(document) != {
        "schema_version",
        "workload_id",
        "definition_hash",
        "artifact",
        "operations",
        "endpoint",
    } or document.get("schema_version") != 1:
        raise ValueError("release manifest schema is invalid")
    if document.get("workload_id") != workload_id:
        raise ValueError("release workload reference is invalid")
    if document.get("definition_hash") != definition_hash:
        raise ValueError("release definition hash does not match workload")
    artifact = _mapping(document["artifact"], "release artifact")
    if (
        set(artifact) != {"reference", "sha256"}
        or not isinstance(artifact.get("reference"), str)
        or _ARTIFACT.fullmatch(cast(str, artifact["reference"])) is None
        or not isinstance(artifact.get("sha256"), str)
        or _DIGEST.fullmatch(cast(str, artifact["sha256"])) is None
        or not cast(str, artifact["reference"]).endswith(
            "@sha256:" + cast(str, artifact["sha256"])
        )
    ):
        raise ValueError("release artifact is invalid")
    operations = document["operations"]
    if (
        not isinstance(operations, Sequence)
        or isinstance(operations, (str, bytes))
        or len(operations) != len(set(operations))
        or set(operations) != _REQUIRED_CAPABILITIES
    ):
        raise ValueError("release operations are outside the closed agent registry")
    endpoint = _mapping(document["endpoint"], "release endpoint")
    if (
        set(endpoint) != {"scheme", "port", "path"}
        or endpoint.get("scheme") not in {"http", "https"}
        or not isinstance(endpoint.get("port"), int)
        or isinstance(endpoint.get("port"), bool)
        or not 1 <= cast(int, endpoint["port"]) <= 65535
        or not isinstance(endpoint.get("path"), str)
        or not cast(str, endpoint["path"]).startswith("/")
    ):
        raise ValueError("release endpoint schema is invalid")
    return MappingProxyType(
        {
            **document,
            "artifact": MappingProxyType(dict(artifact)),
            "operations": tuple(sorted(cast(Sequence[str], operations))),
            "endpoint": MappingProxyType(dict(endpoint)),
        }
    )


def _profile_cross_references(
    profile: Mapping[str, Any], workloads: Mapping[str, Mapping[str, Any]]
) -> None:
    endpoints = _mapping(profile["endpoints"], "profile endpoints")
    if not endpoints or not all(
        _IDENTIFIER.fullmatch(alias) and workload_id in workloads
        for alias, workload_id in endpoints.items()
    ):
        raise ValueError("profile route reference is invalid")
    selected = set(workloads)
    for workload_id, workload in workloads.items():
        conflicts = set(cast(Sequence[str], workload["conflicts"]))
        if conflicts & (selected - {workload_id}):
            raise ValueError("profile selects conflicting workloads")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _placement_observations(
    observations: Iterable[DesiredStateObservation],
    *,
    fleet: Fleet,
    now: datetime,
    ttl: timedelta,
) -> dict[NodeId, NodeObservation]:
    now = _aware(now)
    resolved: dict[NodeId, NodeObservation] = {}
    for observation in observations:
        node_id = NodeId.parse(observation.node_id)
        if node_id in resolved:
            raise ValueError("duplicate durable observation")
        age = now - _aware(observation.observed_at)
        if age < timedelta(0) or age > ttl:
            raise ValueError("node observation is stale")
        if observation.agent_state != "active":
            raise ValueError("node does not have a connected active agent")
        if observation.protocol_version != _SUPPORTED_PROTOCOL_RANGE[0]:
            raise ValueError("agent protocol version is incompatible")
        capabilities = set(observation.capabilities)
        if not _REQUIRED_CAPABILITIES <= capabilities <= _IMPLEMENTED_CAPABILITIES:
            raise ValueError("agent capabilities are incompatible")
        resolved[node_id] = NodeObservation(
            node_id,
            observation.healthy,
            observation.memory_available_bytes,
            observation.disk_available_bytes,
            observation.occupied,
        )
    if set(resolved) != set(fleet.nodes):
        raise ValueError("durable observations must exactly cover the fleet")
    return resolved


def _routes(
    profile: Mapping[str, Any],
    placements: Mapping[str, tuple[str, ...]],
    releases: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for alias, workload_id in sorted(
        _mapping(profile["endpoints"], "profile endpoints").items()
    ):
        endpoint = cast(Mapping[str, object], releases[workload_id]["endpoint"])
        result[alias] = MappingProxyType(
            {
                "workload_id": workload_id,
                "nodes": placements[workload_id],
                "scheme": endpoint["scheme"],
                "port": endpoint["port"],
                "path": endpoint["path"],
            }
        )
    return MappingProxyType(result)


def _payload_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _operations(
    commit: str,
    *,
    placements: Mapping[str, tuple[str, ...]],
    releases: Mapping[str, Mapping[str, Any]],
    placement_inputs: Mapping[str, str],
    lifecycle: Mapping[str, Any],
) -> tuple[OperationGraph, Mapping[str, Mapping[str, object]]]:
    nodes: dict[str, OperationNode] = {}
    payloads: dict[str, Mapping[str, object]] = {}
    for workload_id, targets in sorted(placements.items()):
        operation_ids: dict[tuple[str, str], str] = {}
        for node_id in targets:
            for kind in _PLANNED_OPERATIONS:
                operation_ids[(node_id, kind)] = f"{workload_id}:{node_id}:{kind}"
        worker_starts = tuple(
            operation_ids[(node_id, AgentOperation.WORKLOAD_START.value)]
            for node_id in targets[1:]
        )
        for node_id in targets:
            for kind in _PLANNED_OPERATIONS:
                operation_id = operation_ids[(node_id, kind)]
                if kind == AgentOperation.RELEASE_INSTALL.value:
                    dependencies: tuple[str, ...] = ()
                elif kind == AgentOperation.WORKLOAD_PREPARE.value:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.RELEASE_INSTALL.value)],
                    )
                elif kind == AgentOperation.WORKLOAD_START.value:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.WORKLOAD_PREPARE.value)],
                    )
                    if (
                        node_id == targets[0]
                        and lifecycle["start_order"] == "workers-before-entrypoint"
                    ):
                        dependencies += worker_starts
                elif kind == AgentOperation.WORKLOAD_HEALTH.value:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.WORKLOAD_START.value)],
                    )
                else:
                    dependencies = (
                        operation_ids[(node_id, AgentOperation.WORKLOAD_HEALTH.value)],
                    )
                payload: Mapping[str, object] = {
                    "schema_version": 1,
                    "workload_id": workload_id,
                    "node_id": node_id,
                    "operation": kind,
                    "definition_hash": releases[workload_id]["definition_hash"],
                    "release_manifest": releases[workload_id]["manifest_path"],
                    "release_manifest_sha256": releases[workload_id][
                        "manifest_sha256"
                    ],
                    "placement_input_digest": placement_inputs[workload_id],
                }
                payloads[operation_id] = MappingProxyType(dict(payload))
                nodes[operation_id] = OperationNode(
                    operation_id=operation_id,
                    node_id=node_id,
                    workload_id=workload_id,
                    kind=kind,
                    dependencies=tuple(sorted(set(dependencies))),
                    compensation_kind=(
                        AgentOperation.WORKLOAD_STOP.value
                        if kind == AgentOperation.WORKLOAD_START.value
                        else None
                    ),
                    payload_digest=_payload_digest(payload),
                )
    ordered = _topological(nodes)
    targets = tuple(sorted({node.node_id for node in ordered}))
    graph_document = {
        "schema_version": 1,
        "base_commit": commit,
        "targets": list(targets),
        "nodes": [node.to_document() for node in ordered],
    }
    digest = hashlib.sha256(
        json.dumps(graph_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        OperationGraph("", commit, targets, ordered, digest),
        MappingProxyType(dict(sorted(payloads.items()))),
    )


def _topological(nodes: Mapping[str, OperationNode]) -> tuple[OperationNode, ...]:
    unresolved = {
        operation_id: set(node.dependencies) for operation_id, node in nodes.items()
    }
    ordered: list[OperationNode] = []
    while unresolved:
        ready = sorted(
            operation_id
            for operation_id, dependencies in unresolved.items()
            if not dependencies
        )
        if not ready:
            raise ValueError("resolved operation graph contains a cycle")
        for operation_id in ready:
            ordered.append(nodes[operation_id])
            del unresolved[operation_id]
        for dependencies in unresolved.values():
            dependencies.difference_update(ready)
    return tuple(ordered)
