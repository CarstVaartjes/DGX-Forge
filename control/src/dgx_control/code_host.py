"""Code-host abstraction; production credentials stay behind provider references."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


class CodeHost(Protocol):
    def create_change(self, branch: str, base_commit: str, patch: bytes, message: str, *, signed: bool) -> str: ...
    def open_pull_request(self, branch: str, commit: str, title: str) -> str: ...
    def reachable_from(self, commit: str, branch: str) -> bool: ...
    def check_state(self, commit: str, check: str) -> str | None: ...


@dataclass(frozen=True)
class HostedChange:
    commit: str
    branch: str
    pull_request: str | None


class InMemoryCodeHost:
    """Deterministic policy fake; never represents a production credential store."""

    def __init__(self, *, required_checks: tuple[str, ...]) -> None:
        self._branches: dict[str, tuple[str, bytes, str, str]] = {}
        self._pull_requests: dict[str, str] = {}
        self._merged: set[str] = set()
        self._checks: dict[str, dict[str, str]] = {}
        self.required_checks = required_checks
        self.submission_count = 0
        self.last_message = ""

    def create_change(self, branch: str, base_commit: str, patch: bytes, message: str, *, signed: bool) -> str:
        if not signed:
            raise ValueError("control-plane commits must be signed")
        existing = self._branches.get(branch)
        identity = (base_commit, patch, message)
        if existing:
            if existing[:3] != identity:
                raise ValueError("refusing force update of an existing control branch")
            return existing[3]
        commit = hashlib.sha1(base_commit.encode() + b"\0" + patch + b"\0" + message.encode()).hexdigest()
        self._branches[branch] = (*identity, commit)
        self._checks.setdefault(commit, {})
        self.submission_count += 1
        self.last_message = message
        return commit

    def open_pull_request(self, branch: str, commit: str, title: str) -> str:
        if branch not in self._branches or self._branches[branch][3] != commit:
            raise ValueError("pull request branch does not contain the proposed commit")
        return self._pull_requests.setdefault(branch, f"pr://{branch}")

    def reachable_from(self, commit: str, branch: str) -> bool:
        return commit in self._merged

    def check_state(self, commit: str, check: str) -> str | None:
        return self._checks.get(commit, {}).get(check)

    def seed_commit(self, *, merged: bool, checks: dict[str, str]) -> str:
        commit = hashlib.sha1(repr((len(self._checks), checks)).encode()).hexdigest()
        self._checks[commit] = dict(checks)
        if merged:
            self._merged.add(commit)
        return commit

    def set_check(self, commit: str, check: str, state: str) -> None:
        self._checks.setdefault(commit, {})[check] = state
