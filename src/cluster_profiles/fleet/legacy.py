"""Read-only adaptation of the original two-GPU node inventory format."""

from __future__ import annotations

import tomllib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .types import Fleet, ManagementEndpoint, NodeId, NodeRecord

_PROJECT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://github.com/CarstVaartjes/vonk-forge",
)


class LegacyFleetError(ValueError):
    """The legacy inventory cannot be adapted safely."""


def _table(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise LegacyFleetError(f"legacy {field} must be a table")
    return cast(Mapping[str, Any], value)


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyFleetError(f"legacy {field} must be a nonblank string")
    return value


def _legacy_node_id(legacy_key: str) -> NodeId:
    generated = uuid.uuid5(_PROJECT_NAMESPACE, f"legacy-node:{legacy_key}")
    return NodeId.parse(f"spk_{generated.hex}")


def load_legacy_cluster(path: Path) -> Fleet:
    """Adapt legacy host tables without modifying or rewriting the source."""

    try:
        with path.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LegacyFleetError(
            f"could not load legacy inventory {path}: {type(error).__name__}"
        ) from None

    root = _table(document, field="inventory")
    hosts = _table(root.get("hosts"), field="hosts")
    if not hosts:
        raise LegacyFleetError("legacy inventory must contain at least one host")

    records: list[NodeRecord] = []
    aliases: list[str] = []
    for legacy_key, raw_host in hosts.items():
        host = _table(raw_host, field=f"host {legacy_key}")
        hostname = _text(host.get("hostname"), field=f"host {legacy_key} hostname")
        alias = _text(host.get("ssh_alias"), field=f"host {legacy_key} ssh_alias")
        role = host.get("role")
        labels = {"legacy_key": legacy_key}
        if role is not None:
            labels["legacy_role"] = _text(
                role, field=f"host {legacy_key} role"
            )
        records.append(
            NodeRecord(
                id=_legacy_node_id(legacy_key),
                display_name=legacy_key,
                hostname=hostname,
                management=ManagementEndpoint(
                    host=alias,
                    user="legacy-ssh-config",
                ),
                labels=labels,
                lifecycle="ready",
            )
        )
        aliases.append(alias)

    if len(aliases) != len(set(aliases)):
        raise LegacyFleetError("legacy SSH aliases must be unique")
    try:
        return Fleet(
            schema_version=2,
            nodes={record.id: record for record in records},
        )
    except ValueError as error:
        raise LegacyFleetError(str(error)) from None
