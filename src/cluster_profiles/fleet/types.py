"""Immutable, address-independent domain types for Vonk Forge GPU node fleets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}")
_LIFECYCLES = frozenset(
    {"discovered", "installing", "ready", "quarantined", "draining", "retired"}
)

NodeLifecycle = Literal[
    "discovered",
    "installing",
    "ready",
    "quarantined",
    "draining",
    "retired",
]


@dataclass(frozen=True, order=True)
class NodeId:
    """Stable generated identity that is independent of mutable node metadata."""

    value: str

    @classmethod
    def parse(cls, value: str) -> NodeId:
        if _NODE_ID.fullmatch(value) is None:
            raise ValueError(
                "node id must match spk_<32 lowercase hex characters>"
            )
        return cls(value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ManagementEndpoint:
    """Connection metadata; credentials are referenced, never embedded."""

    host: str
    user: str
    port: int = 22
    credential_ref: str | None = None


@dataclass(frozen=True)
class NodeRecord:
    """Sanitized desired record for one physical Vonk Forge GPU node."""

    id: NodeId
    display_name: str
    hostname: str
    management: ManagementEndpoint
    labels: Mapping[str, str]
    lifecycle: NodeLifecycle

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("node display name must not be blank")
        if not self.hostname.strip():
            raise ValueError("node hostname must not be blank")
        if not self.management.host.strip():
            raise ValueError("management host must not be blank")
        if not self.management.user.strip():
            raise ValueError("management user must not be blank")
        if not 1 <= self.management.port <= 65535:
            raise ValueError("management port must be between 1 and 65535")
        if self.lifecycle not in _LIFECYCLES:
            raise ValueError(f"unsupported node lifecycle: {self.lifecycle}")
        labels = dict(self.labels)
        if any(not key.strip() or not value.strip() for key, value in labels.items()):
            raise ValueError("node label keys and values must not be blank")
        object.__setattr__(self, "labels", MappingProxyType(labels))


@dataclass(frozen=True)
class Fleet:
    """A nonempty, versioned fleet with no fixed node count."""

    schema_version: int
    nodes: Mapping[NodeId, NodeRecord]

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError(
                f"unsupported fleet schema version: {self.schema_version}"
            )
        nodes = dict(self.nodes)
        if not nodes:
            raise ValueError("fleet must contain at least one node")
        for node_id, record in nodes.items():
            if node_id != record.id:
                raise ValueError("fleet node key must match the node record id")
        display_names = [record.display_name for record in nodes.values()]
        if len(display_names) != len(set(display_names)):
            raise ValueError("fleet node display names must be unique")
        object.__setattr__(self, "nodes", MappingProxyType(nodes))

    def node(self, node_id: NodeId) -> NodeRecord:
        return self.nodes[node_id]

    def ready_nodes(self) -> tuple[NodeRecord, ...]:
        return tuple(
            self.nodes[node_id]
            for node_id in sorted(self.nodes)
            if self.nodes[node_id].lifecycle == "ready"
        )
