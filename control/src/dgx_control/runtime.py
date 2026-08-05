"""Fail-closed production handlers for commit-pinned cluster jobs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .worker import HandlerRequest

_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def run_bounded(arguments: Sequence[str]) -> Mapping[str, object]:
    process = subprocess.run(
        tuple(arguments), stdin=subprocess.DEVNULL, capture_output=True,
        timeout=86_400, check=False, shell=False,
    )
    stdout = process.stdout[:65_536]
    stderr = process.stderr[:65_536]
    if process.returncode != 0:
        raise RuntimeError(f"command failed with exit status {process.returncode}: {stderr.decode('utf-8', 'replace')}")
    try:
        parsed = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {"output": stdout.decode("utf-8", "replace")}
    return parsed if isinstance(parsed, Mapping) else {"output": parsed}


class RuntimeHandlers:
    def __init__(
        self,
        repository_root: Path,
        *,
        eligible: Callable[[str], bool],
        current_commit: Callable[[], str] | None = None,
        run: Callable[[Sequence[str]], Mapping[str, object]] = run_bounded,
        route_manager=None,
    ) -> None:
        self._root = repository_root.resolve()
        self._eligible = eligible
        self._current_commit = current_commit or self._resolve_current_commit
        self._run = run
        self._route_manager = route_manager

    def _resolve_current_commit(self) -> str:
        process = subprocess.run(
            ("git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-C", str(self._root), "rev-parse", "--verify", "HEAD^{commit}"),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False, shell=False,
        )
        commit = process.stdout.decode("ascii", "replace").strip()
        if process.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ValueError("repository checkout commit cannot be resolved")
        return commit

    def current_commit(self) -> str:
        return self._current_commit()

    def registry(self) -> Mapping[str, Callable[[HandlerRequest], Mapping[str, object]]]:
        return {"probe": self.probe, "reconcile": self.reconcile}

    def probe(self, request: HandlerRequest) -> Mapping[str, object]:
        if request.payload:
            raise ValueError("probe payload must be empty")
        return self._run((str(self._root / "bin/sparkctl"), "nodes", "status", "--json"))

    def reconcile(self, request: HandlerRequest) -> Mapping[str, object]:
        if not self._eligible(request.base_commit):
            raise ValueError("reconciliation commit is no longer eligible")
        if self._current_commit() != request.base_commit:
            raise ValueError("repository checkout does not match the pinned commit")
        payload = dict(request.payload)
        digest = payload.pop("plan_digest", None)
        placements = payload.get("placements")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("reconciliation plan digest is invalid")
        if not isinstance(placements, Mapping):
            raise ValueError("reconciliation placements are invalid")
        content = {
            "commit": request.base_commit,
            "targets": sorted(request.targets),
            "placements": placements,
            "routes": payload.get("routes", {}),
            "releases": payload.get("releases", {}),
            "input_digests": payload.get("input_digests", {}),
        }
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        if not hashlib.sha256(encoded).hexdigest() == digest:
            raise ValueError("reconciliation plan digest does not match queued content")
        profile = placements.get("profile")
        workloads = placements.get("workloads")
        releases = content["releases"]
        if not isinstance(profile, str) or _NAME.fullmatch(profile) is None:
            raise ValueError("reconciliation profile is invalid")
        if not isinstance(workloads, Mapping) or not isinstance(releases, Mapping):
            raise ValueError("reconciliation workloads or releases are invalid")
        if set(workloads) != set(releases):
            raise ValueError("reconciliation workloads and releases differ")
        routes = content["routes"]
        if not isinstance(routes, Mapping):
            raise ValueError("reconciliation routes are invalid")
        if self._route_manager is not None:
            self._route_manager.withdraw(request.targets)
        commands = 0
        for workload in sorted(workloads):
            release = releases[workload]
            if not isinstance(workload, str) or _NAME.fullmatch(workload) is None or not isinstance(release, str) or _SHA256.fullmatch(release) is None:
                raise ValueError("reconciliation workload release is invalid")
            self._run((str(self._root / "scripts/deploy-runtime-release"), workload, "--root", str(self._root), "--apply"))
            commands += 1
        self._run((str(self._root / "bin/sparkctl"), "switch", profile, "--json"))
        if self._route_manager is not None:
            self._route_manager.publish(
                commit=request.base_commit,
                profile=profile,
                targets=request.targets,
                routes=routes,
            )
        return {"commit": request.base_commit, "plan_digest": digest, "commands": commands + 1}
