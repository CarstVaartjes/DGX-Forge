"""One-way release policy and commit eligibility checks."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .code_host import CodeHost
from .proposals import ProposalPreview


class IrreversiblePolicyError(RuntimeError):
    pass


class ReleaseGateError(RuntimeError):
    pass


class PolicyStore:
    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise ValueError("policy state root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self._marker = root / "release-pr-only.json"
        if self._marker.is_symlink():
            raise ValueError("release policy marker must not be a symlink")

    @property
    def mode(self) -> str:
        return "release-pr-only" if self._marker.is_file() else "development-direct"

    def enable_release_pr_only(self, *, actor: str, release_digest: str, release_status: str) -> None:
        if not actor.strip():
            raise ValueError("release policy transition requires an actor")
        if release_status != "passed" or re.fullmatch(r"[0-9a-f]{64}", release_digest) is None:
            raise ReleaseGateError("PR-only transition requires passed content-addressed release evidence")
        content = (json.dumps({"mode": "release-pr-only", "actor": actor, "release_digest": release_digest}, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            descriptor = os.open(self._marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

    def enable_development_direct(self, *, actor: str) -> None:
        if self.mode == "release-pr-only":
            raise IrreversiblePolicyError("release PR-only policy cannot return to direct mode")
        if not actor.strip():
            raise ValueError("policy action requires an actor")


@dataclass(frozen=True)
class SubmittedChange:
    proposal_digest: str
    commit: str
    branch: str
    mode: str
    pull_request: str | None


@dataclass(frozen=True)
class Eligibility:
    commit: str
    ok: bool
    reasons: tuple[str, ...]


class GitPolicy:
    def __init__(self, store: PolicyStore, code_host: CodeHost, *, protected_branch: str, required_checks: tuple[str, ...]) -> None:
        if not protected_branch.strip() or len(required_checks) != len(set(required_checks)):
            raise ValueError("protected branch and unique required checks are required")
        self._store = store
        self._host = code_host
        self._branch = protected_branch
        self._checks = required_checks
        self._submitted: dict[str, SubmittedChange] = {}

    def submit(self, preview: ProposalPreview, *, actor: str, request_id: str) -> SubmittedChange:
        if preview.digest in self._submitted:
            return self._submitted[preview.digest]
        if re.fullmatch(r"[0-9a-f]{64}", preview.digest) is None:
            raise ValueError("proposal digest is invalid")
        message = (
            "Vonk Forge control proposal\n\n"
            f"Actor: {actor}\nRequest-ID: {request_id}\nProposal-Digest: {preview.digest}\n"
        )
        if self._store.mode == "release-pr-only":
            branch = f"vonk-control/{preview.digest[:12]}"
            commit = self._host.create_change(branch, preview.base_commit, preview.patch, message, signed=True)
            pull_request = self._host.open_pull_request(branch, commit, f"Vonk Forge control proposal {preview.digest[:12]}")
            mode = "pull-request"
        else:
            branch = self._branch
            commit = self._host.create_change(branch, preview.base_commit, preview.patch, message, signed=True)
            pull_request = None
            mode = "direct-commit"
        submitted = SubmittedChange(preview.digest, commit, branch, mode, pull_request)
        self._submitted[preview.digest] = submitted
        return submitted

    def eligible(self, commit: str) -> Eligibility:
        reasons: list[str] = []
        if not self._host.reachable_from(commit, self._branch):
            reasons.append("not reachable from protected deployment branch")
        for check in self._checks:
            state = self._host.check_state(commit, check)
            if state is None:
                reasons.append(f"required check {check} is missing")
            elif state != "success":
                reasons.append(f"required check {check} is {state}")
        return Eligibility(commit, not reasons, tuple(reasons))
