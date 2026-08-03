"""Strict loaders and cross-reference checks for generic fleet documents."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .types import Fleet, ManagementEndpoint, NodeId, NodeLifecycle, NodeRecord


class FleetLoadError(ValueError):
    """A fleet file is unreadable or violates the versioned contract."""


class TopologyValidationError(ValueError):
    """A topology is structurally valid but its references are inconsistent."""


def validate_topology_references(document: Mapping[str, object]) -> None:
    """Reject endpoint references and link identities that are not self-consistent."""

    raw_nodes = document.get("nodes")
    raw_links = document.get("links")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise TopologyValidationError("topology nodes must be a sequence")
    if not isinstance(raw_links, Sequence) or isinstance(raw_links, (str, bytes)):
        raise TopologyValidationError("topology links must be a sequence")

    nodes = set(raw_nodes)
    seen_links: set[object] = set()
    for raw_link in raw_links:
        if not isinstance(raw_link, Mapping):
            raise TopologyValidationError("topology link must be an object")
        link_id = raw_link.get("id")
        if link_id in seen_links:
            raise TopologyValidationError(f"duplicate link id: {link_id}")
        seen_links.add(link_id)
        endpoints = raw_link.get("endpoints")
        if not isinstance(endpoints, Sequence) or isinstance(
            endpoints, (str, bytes)
        ):
            raise TopologyValidationError(f"link {link_id} endpoints must be a sequence")
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                raise TopologyValidationError(
                    f"link {link_id} endpoint must be an object"
                )
            node_id = endpoint.get("node_id")
            if node_id not in nodes:
                raise TopologyValidationError(
                    f"link {link_id} references unknown node {node_id}"
                )


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise FleetLoadError(f"{field} must be a table")
    return cast(Mapping[str, Any], value)


def _exact_keys(
    value: Mapping[str, Any],
    *,
    field: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise FleetLoadError(f"{field} is missing required fields")
    if unknown:
        raise FleetLoadError(f"{field} contains unknown fields")


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FleetLoadError(f"{field} must be a nonblank string")
    return value


def _node_record(node_id_text: str, raw: object) -> NodeRecord:
    node = _mapping(raw, field=f"node {node_id_text}")
    _exact_keys(
        node,
        field=f"node {node_id_text}",
        required={"display_name", "hostname", "management", "labels", "lifecycle"},
    )
    management = _mapping(
        node["management"], field=f"node {node_id_text} management"
    )
    _exact_keys(
        management,
        field=f"node {node_id_text} management",
        required={"host", "user", "port"},
        optional={"credential_ref"},
    )
    port = management["port"]
    if not isinstance(port, int) or isinstance(port, bool):
        raise FleetLoadError(f"node {node_id_text} management port must be an integer")
    credential_ref = management.get("credential_ref")
    if credential_ref is not None:
        credential_ref = _text(
            credential_ref,
            field=f"node {node_id_text} management credential_ref",
        )
    labels = _mapping(node["labels"], field=f"node {node_id_text} labels")
    parsed_labels = {
        _text(key, field=f"node {node_id_text} label key"): _text(
            value, field=f"node {node_id_text} label value"
        )
        for key, value in labels.items()
    }
    try:
        return NodeRecord(
            id=NodeId.parse(node_id_text),
            display_name=_text(
                node["display_name"], field=f"node {node_id_text} display_name"
            ),
            hostname=_text(
                node["hostname"], field=f"node {node_id_text} hostname"
            ),
            management=ManagementEndpoint(
                host=_text(
                    management["host"],
                    field=f"node {node_id_text} management host",
                ),
                user=_text(
                    management["user"],
                    field=f"node {node_id_text} management user",
                ),
                port=port,
                credential_ref=credential_ref,
            ),
            labels=parsed_labels,
            lifecycle=cast(NodeLifecycle, node["lifecycle"]),
        )
    except ValueError as error:
        raise FleetLoadError(str(error)) from None


def load_fleet(path: Path) -> Fleet:
    """Load a v2 fleet without resolving or mutating any node or source file."""

    try:
        with path.open("rb") as source:
            raw = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise FleetLoadError(f"could not load fleet file {path}: {type(error).__name__}") from None
    root = _mapping(raw, field="fleet")
    _exact_keys(root, field="fleet", required={"schema_version", "nodes"})
    if root["schema_version"] != 2:
        raise FleetLoadError(
            f"unsupported fleet schema version: {root['schema_version']!r}"
        )
    raw_nodes = _mapping(root["nodes"], field="fleet nodes")
    records = [_node_record(node_id, node) for node_id, node in raw_nodes.items()]
    try:
        return Fleet(schema_version=2, nodes={record.id: record for record in records})
    except ValueError as error:
        raise FleetLoadError(str(error)) from None
