"""Closed declarative protocol for digest-bound recipe lifecycle work."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AgentOperation,
    AgentProtocolError,
    _fields,
    _mapping,
    _uuid,
    _version,
)

RECIPE_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_STOP,
        AgentOperation.RECIPE_UNINSTALL,
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?\Z")


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AgentProtocolError(f"{name} must be a lowercase SHA-256")
    return value


def _bytes(value: object, name: str, *, positive: bool = False) -> int:
    floor = 1 if positive else 0
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not floor <= value <= 16 * 1024**4
    ):
        raise AgentProtocolError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RecipeOperationRequest:
    operation: AgentOperation
    schema_version: int
    plan_digest: str
    installation_id: str | None = None
    recipe_revision_id: str | None = None
    recipe_content_sha256: str | None = None
    expected_bytes: int | None = None
    run_id: str | None = None
    alias: str | None = None
    rank: int | None = None
    role: str | None = None
    port: int | None = None
    reserved_memory_bytes: int | None = None

    @classmethod
    def parse(cls, operation: AgentOperation, payload: Any) -> RecipeOperationRequest:
        if operation not in RECIPE_OPERATIONS:
            raise AgentProtocolError("recipe operation is not supported")
        value = _mapping(payload)
        common = {"schema_version", "plan_digest"}
        if operation is AgentOperation.RECIPE_INSTALL:
            required = common | {
                "installation_id",
                "recipe_revision_id",
                "recipe_content_sha256",
                "expected_bytes",
            }
        elif operation is AgentOperation.RECIPE_START:
            required = common | {
                "run_id",
                "installation_id",
                "recipe_revision_id",
                "recipe_content_sha256",
                "alias",
                "rank",
                "role",
                "port",
                "reserved_memory_bytes",
            }
        elif operation is AgentOperation.RECIPE_STOP:
            required = common | {"run_id"}
        else:
            required = common | {"installation_id", "recipe_content_sha256"}
        _fields(value, required=required)
        schema_version = _version(value["schema_version"])
        plan_digest = _digest(value["plan_digest"], "plan_digest")
        installation_id = (
            _uuid(value["installation_id"], name="installation_id")
            if "installation_id" in value
            else None
        )
        recipe_revision_id = (
            _uuid(value["recipe_revision_id"], name="recipe_revision_id")
            if "recipe_revision_id" in value
            else None
        )
        recipe_digest = (
            _digest(value["recipe_content_sha256"], "recipe_content_sha256")
            if "recipe_content_sha256" in value
            else None
        )
        expected_bytes = (
            _bytes(value["expected_bytes"], "expected_bytes")
            if "expected_bytes" in value
            else None
        )
        run_id = (
            _uuid(value["run_id"], name="run_id") if "run_id" in value else None
        )
        alias = value.get("alias")
        rank = value.get("rank")
        role = value.get("role")
        port = value.get("port")
        reserved_memory = value.get("reserved_memory_bytes")
        if operation is AgentOperation.RECIPE_START:
            if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
                raise AgentProtocolError("recipe alias is invalid")
            if (
                not isinstance(rank, int)
                or isinstance(rank, bool)
                or not 0 <= rank <= 1023
                or role not in {"entrypoint", "worker"}
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not 1024 <= port <= 65535
            ):
                raise AgentProtocolError("recipe start placement is invalid")
            reserved_memory = _bytes(
                reserved_memory, "reserved_memory_bytes", positive=True
            )
        return cls(
            operation=operation,
            schema_version=schema_version,
            plan_digest=plan_digest,
            installation_id=installation_id,
            recipe_revision_id=recipe_revision_id,
            recipe_content_sha256=recipe_digest,
            expected_bytes=expected_bytes,
            run_id=run_id,
            alias=alias if isinstance(alias, str) else None,
            rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
            role=role if isinstance(role, str) else None,
            port=port if isinstance(port, int) and not isinstance(port, bool) else None,
            reserved_memory_bytes=reserved_memory if isinstance(reserved_memory, int) else None,
        )


__all__ = ["RECIPE_OPERATIONS", "RecipeOperationRequest"]
