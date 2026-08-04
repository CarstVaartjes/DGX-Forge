"""Atomic fail-closed publication of accepted inference routes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_COMMIT = re.compile(r"[0-9a-f]{40}")
_NODE = re.compile(r"spk_[0-9a-f]{32}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


class RouteValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RouteCandidate:
    commit: str
    profile: str
    workload: str
    node_ids: tuple[str, ...]
    aliases: Mapping[str, str]
    health_timestamp: datetime


@dataclass(frozen=True)
class RouteState:
    generation: int
    state: str
    commit: str | None
    profile: str | None
    workload: str | None
    node_ids: tuple[str, ...]
    aliases: Mapping[str, str]
    health_timestamp: str | None
    reason: str | None
    digest: str


class RoutePublisher:
    def __init__(
        self,
        root: Path,
        *,
        allowed_upstreams: AbstractSet[str],
        validate: Callable[[bytes], bool],
        apply: Callable[[bytes], None],
    ) -> None:
        if root.is_symlink():
            raise RouteValidationError("route state root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self._root = root
        self._generations = root / "generations"
        if self._generations.is_symlink():
            raise RouteValidationError("route generations must not be a symlink")
        self._generations.mkdir(mode=0o700, exist_ok=True)
        self._allowed = frozenset(allowed_upstreams)
        self._validate = validate
        self._apply = apply
        self._state: RouteState | None = None
        self._load()

    @staticmethod
    def _encoded(payload: Mapping[str, object]) -> bytes:
        return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def _load(self) -> None:
        active = self._root / "active.json"
        if not active.exists():
            return
        if active.is_symlink() or not active.is_file():
            raise RouteValidationError("active route pointer is unsafe")
        try:
            pointer = json.loads(active.read_bytes())
            generation = self._generations / pointer["directory"] / "routes.json"
            content = generation.read_bytes()
            if hashlib.sha256(content).hexdigest() != pointer["sha256"]:
                raise RouteValidationError("active route generation checksum mismatch")
            payload = json.loads(content)
            self._state = RouteState(
                generation=payload["generation"], state=payload["state"], commit=payload.get("commit"),
                profile=payload.get("profile"), workload=payload.get("workload"),
                node_ids=tuple(payload["node_ids"]), aliases=dict(payload["aliases"]),
                health_timestamp=payload.get("health_timestamp"), reason=payload.get("reason"),
                digest=pointer["sha256"],
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RouteValidationError("active route state is unreadable") from error

    def _publish_payload(self, payload: dict[str, object]) -> RouteState:
        generation = (self._state.generation if self._state else 0) + 1
        payload["generation"] = generation
        content = self._encoded(payload)
        if self._validate(content) is not True:
            raise RouteValidationError("route candidate failed configuration validation")
        digest = hashlib.sha256(content).hexdigest()
        directory_name = f"{generation:08d}-{digest}"
        directory = self._generations / directory_name
        try:
            directory.mkdir(mode=0o700)
            target = directory / "routes.json"
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content); output.flush(); os.fsync(output.fileno())
            self._apply(content)
        except RouteValidationError:
            raise
        except Exception as error:
            raise RouteValidationError("route candidate apply failed; previous generation retained") from error
        pointer = self._encoded({"directory": directory_name, "sha256": digest})
        descriptor, temporary_raw = tempfile.mkstemp(prefix=".active-", dir=self._root)
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(pointer); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self._root / "active.json")
        finally:
            temporary.unlink(missing_ok=True)
        self._state = RouteState(
            generation=generation, state=str(payload["state"]), commit=payload.get("commit"),
            profile=payload.get("profile"), workload=payload.get("workload"),
            node_ids=tuple(payload["node_ids"]), aliases=dict(payload["aliases"]),
            health_timestamp=payload.get("health_timestamp"), reason=payload.get("reason"), digest=digest,
        )
        return self._state

    def maintenance(self, targets: tuple[str, ...], reason: str) -> RouteState:
        if not targets or any(_NODE.fullmatch(target) is None for target in targets):
            raise RouteValidationError("maintenance targets must be stable node IDs")
        safe_reason = re.sub(r"(?i)(bearer|token|secret|password)\S*", "<redacted>", reason)[:256]
        return self._publish_payload({
            "state": "maintenance", "commit": None, "profile": None, "workload": None,
            "node_ids": sorted(set(targets)), "aliases": {}, "health_timestamp": None,
            "reason": safe_reason or "maintenance",
        })

    def publish(self, candidate: RouteCandidate) -> RouteState:
        if _COMMIT.fullmatch(candidate.commit) is None or _NAME.fullmatch(candidate.profile) is None or _NAME.fullmatch(candidate.workload) is None:
            raise RouteValidationError("route candidate identity is invalid")
        if not candidate.node_ids or any(_NODE.fullmatch(node) is None for node in candidate.node_ids):
            raise RouteValidationError("route candidate nodes are invalid")
        if candidate.health_timestamp.tzinfo is None or candidate.health_timestamp.utcoffset() is None:
            raise RouteValidationError("route health timestamp must be timezone-aware")
        aliases = dict(candidate.aliases)
        if not aliases or any(_NAME.fullmatch(alias) is None for alias in aliases):
            raise RouteValidationError("route aliases are invalid")
        unknown = set(aliases.values()) - self._allowed
        if unknown:
            raise RouteValidationError("route candidate contains an unconfigured upstream")
        return self._publish_payload({
            "state": "published", "commit": candidate.commit, "profile": candidate.profile,
            "workload": candidate.workload, "node_ids": sorted(set(candidate.node_ids)),
            "aliases": dict(sorted(aliases.items())),
            "health_timestamp": candidate.health_timestamp.isoformat(), "reason": None,
        })

    def snapshot(self) -> RouteState:
        if self._state is None:
            raise RouteValidationError("no route generation has been published")
        return self._state

    def visible_aliases(self) -> set[str]:
        return set(self.snapshot().aliases) if self.snapshot().state == "published" else set()
