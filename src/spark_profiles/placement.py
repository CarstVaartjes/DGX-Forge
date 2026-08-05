"""Deterministic requirement-based placement over a generic Spark fleet."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .fleet import Fleet, NodeId
from .fleet.loaders import TopologyValidationError, validate_topology_references


class PlacementError(ValueError):
    """A repository requirement cannot be placed safely."""


@dataclass(frozen=True)
class PlacementRequirement:
    name: str
    definition_hash: str
    node_count: int
    required_labels: Mapping[str, str]
    min_memory_bytes: int
    min_disk_bytes: int
    exclusive: bool
    distributed: bool
    model_supports_distributed: bool
    preferred_node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or re.fullmatch(r"[0-9a-f]{64}", self.definition_hash) is None:
            raise PlacementError("requirement name and definition hash are required")
        if self.node_count < 1 or self.min_memory_bytes < 0 or self.min_disk_bytes < 0:
            raise PlacementError("placement counts and capacities are invalid")
        if self.distributed != (self.node_count > 1):
            raise PlacementError("distributed must exactly match a multi-node requirement")
        labels = dict(self.required_labels)
        if any(not key.strip() or not value.strip() for key, value in labels.items()):
            raise PlacementError("required labels must be nonblank")
        if len(set(self.preferred_node_ids)) != len(self.preferred_node_ids):
            raise PlacementError("preferred node IDs must be unique")
        object.__setattr__(self, "required_labels", MappingProxyType(labels))


@dataclass(frozen=True)
class NodeObservation:
    node_id: NodeId
    healthy: bool
    memory_available_bytes: int
    disk_available_bytes: int
    occupied: bool
    available_for_placement: bool = True

    def __post_init__(self) -> None:
        if self.memory_available_bytes < 0 or self.disk_available_bytes < 0:
            raise PlacementError("observed capacity cannot be negative")
        if not isinstance(self.available_for_placement, bool):
            raise PlacementError("placement availability must be boolean")


@dataclass(frozen=True)
class PlacementPlan:
    requirement: str
    definition_hash: str
    nodes: tuple[NodeId, ...]
    reasons: Mapping[str, tuple[str, ...]]
    input_digest: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class PlacementPlanner:
    def plan(
        self,
        requirement: PlacementRequirement,
        fleet: Fleet,
        topology: Mapping[str, object],
        observations: Iterable[NodeObservation],
    ) -> PlacementPlan:
        if requirement.distributed and not requirement.model_supports_distributed:
            raise PlacementError("model definition does not support distributed placement")
        try:
            validate_topology_references(topology)
        except TopologyValidationError as error:
            raise PlacementError(str(error)) from None
        if set(topology.get("nodes", ())) != {node_id.value for node_id in fleet.nodes}:
            raise PlacementError("topology nodes must exactly match the fleet")
        observed: dict[NodeId, NodeObservation] = {}
        for observation in observations:
            if observation.node_id in observed:
                raise PlacementError("duplicate node observation")
            observed[observation.node_id] = observation
        if set(observed) != set(fleet.nodes):
            raise PlacementError("observations must exactly cover the fleet")
        preferences: dict[str, int] = {}
        for rank, text in enumerate(requirement.preferred_node_ids):
            try:
                preferred = NodeId.parse(text)
            except ValueError:
                raise PlacementError("preferred node ID is invalid") from None
            if preferred not in fleet.nodes:
                raise PlacementError("preferred node is not in the fleet")
            preferences[text] = rank

        reasons: dict[str, tuple[str, ...]] = {}
        eligible: list[NodeId] = []
        for node_id, record in fleet.nodes.items():
            observation = observed[node_id]
            rejected: list[str] = []
            if record.lifecycle != "ready":
                rejected.append(f"lifecycle:{record.lifecycle}")
            if not observation.available_for_placement:
                rejected.append("unavailable")
            if not observation.healthy:
                rejected.append("unhealthy")
            if observation.memory_available_bytes < requirement.min_memory_bytes:
                rejected.append("memory")
            if observation.disk_available_bytes < requirement.min_disk_bytes:
                rejected.append("disk")
            if requirement.exclusive and observation.occupied:
                rejected.append("occupied")
            if any(record.labels.get(key) != value for key, value in requirement.required_labels.items()):
                rejected.append("labels")
            reasons[node_id.value] = tuple(rejected) if rejected else ("eligible",)
            if not rejected:
                eligible.append(node_id)
        eligible.sort(key=lambda node_id: (preferences.get(node_id.value, len(preferences)), node_id.value))
        if len(eligible) < requirement.node_count:
            raise PlacementError("insufficient eligible nodes for requirement")
        selected = tuple(eligible[: requirement.node_count])
        if requirement.distributed and not self._accepted_group(selected, topology):
            raise PlacementError("distributed placement requires accepted topology")
        for node_id in selected:
            reasons[node_id.value] = ("selected",)

        requirement_doc = {
            "name": requirement.name,
            "definition_hash": requirement.definition_hash,
            "node_count": requirement.node_count,
            "min_memory_bytes": requirement.min_memory_bytes,
            "min_disk_bytes": requirement.min_disk_bytes,
            "exclusive": requirement.exclusive,
            "distributed": requirement.distributed,
            "model_supports_distributed": requirement.model_supports_distributed,
            "required_labels": dict(requirement.required_labels),
            "preferred_node_ids": list(requirement.preferred_node_ids),
        }
        observations_doc = [
            {
                "node_id": item.node_id.value,
                "healthy": item.healthy,
                "memory_available_bytes": item.memory_available_bytes,
                "disk_available_bytes": item.disk_available_bytes,
                "occupied": item.occupied,
                "available_for_placement": item.available_for_placement,
            }
            for item in sorted(observed.values(), key=lambda item: item.node_id.value)
        ]
        digest = hashlib.sha256(_canonical({
            "requirement": requirement_doc,
            "fleet": [
                {
                    "id": node_id.value, "lifecycle": record.lifecycle,
                    "labels": dict(sorted(record.labels.items())),
                }
                for node_id, record in sorted(fleet.nodes.items())
            ],
            "topology": topology,
            "observations": observations_doc,
        })).hexdigest()
        return PlacementPlan(
            requirement.name, requirement.definition_hash, selected,
            MappingProxyType(dict(sorted(reasons.items()))), digest,
        )

    @staticmethod
    def _accepted_group(nodes: tuple[NodeId, ...], topology: Mapping[str, object]) -> bool:
        required = {node.value for node in nodes}
        for link in topology.get("links", ()):  # validated structurally above
            if link.get("accepted") and link.get("kind") in {"direct-rdma", "switched-rdma"}:
                linked = {endpoint.get("node_id") for endpoint in link.get("endpoints", ())}
                if required <= linked:
                    return True
        return False
