"""Canonical serializers for allowlisted typed repository documents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import tomli_w

_PRIORITY = {
    "schema_version": 0,
    "id": 1,
    "name": 2,
    "display_name": 3,
    "description": 4,
    "lifecycle": 5,
}
_ADAPTER = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_EXECUTABLE = re.compile(r"/opt/node/model-adapters/[a-z0-9-]+/releases/[0-9a-f]{64}/bin/[A-Za-z0-9._-]+")


def _workload_policy(document: Mapping[str, object]) -> None:
    adapter = document.get("adapter")
    if adapter is not None and (not isinstance(adapter, str) or _ADAPTER.fullmatch(adapter) is None):
        raise ValueError("workload adapter must be a trusted identifier, not a path")
    for forbidden in ("upstream", "api_base", "upstream_url"):
        if forbidden in document:
            raise ValueError("repository workload cannot directly select a network upstream")
    endpoint = document.get("endpoint")
    if endpoint is not None:
        host = endpoint.get("host") if isinstance(endpoint, Mapping) else None
        if not isinstance(host, str) or host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("workload endpoint must remain node-local")
    commands = document.get("commands")
    if commands is not None:
        if not isinstance(commands, Mapping) or not commands:
            raise ValueError("workload commands must be a typed command mapping")
        for command in commands.values():
            if not isinstance(command, list) or not command or not isinstance(command[0], str) or _EXECUTABLE.fullmatch(command[0]) is None:
                raise ValueError("workload command executable is outside immutable adapter releases")


def _ordered(value: object) -> object:
    if isinstance(value, Mapping):
        keys = sorted(value, key=lambda key: (_PRIORITY.get(str(key), 100), str(key)))
        return {str(key): _ordered(value[key]) for key in keys}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported typed document value: {type(value).__name__}")


def serialize_document(path: str, document: Mapping[str, object]) -> bytes:
    if path.startswith("config/workloads/"):
        _workload_policy(document)
    ordered = _ordered(document)
    assert isinstance(ordered, dict)
    if path.endswith(".json"):
        return (json.dumps(ordered, ensure_ascii=False, sort_keys=False, indent=2) + "\n").encode()
    if path.endswith(".toml"):
        return tomli_w.dumps(ordered, multiline_strings=False).replace("\r\n", "\n").encode()
    raise ValueError("typed proposals support only JSON and TOML documents")
