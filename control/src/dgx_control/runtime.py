"""Fail-closed production handlers for commit-pinned cluster jobs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .git_content import read_commit_file
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
        repository_reader: Callable[[str, str], bytes] | None = None,
    ) -> None:
        self._root = repository_root.resolve()
        self._eligible = eligible
        self._current_commit = current_commit or self._resolve_current_commit
        self._run = run
        self._route_manager = route_manager
        self._repository_reader = repository_reader or (
            lambda commit, path: read_commit_file(self._root, commit, path)
        )

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

    def _verify_repository_plan(self, content: Mapping[str, object]) -> None:
        try:
            raw = self._repository_reader(
                str(content["commit"]),
                "inventory/reconciliation.json",
            )
            definition = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("repository plan is unreadable") from error
        if not isinstance(definition, Mapping):
            raise ValueError("repository plan must be an object")
        targets = definition.get("targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(target, str) and target.strip() for target in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("repository plan targets are invalid")
        expected = {
            "commit": content["commit"],
            "targets": sorted(targets),
            "placements": definition.get("placements", {}),
            "routes": definition.get("routes", {}),
            "releases": definition.get("releases", {}),
            "input_digests": definition.get("input_digests", {}),
        }
        if any(
            not isinstance(expected[field], Mapping)
            for field in ("placements", "routes", "releases", "input_digests")
        ):
            raise ValueError("repository plan mappings are invalid")
        if expected != dict(content):
            raise ValueError("queued content does not match the repository plan")

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
            raise TypeError("reconciliation placements are invalid")
        workloads = placements.get("workloads")
        releases = payload.get("releases", {})
        if not isinstance(workloads, Mapping) or not isinstance(releases, Mapping):
            raise TypeError("reconciliation workloads or releases are invalid")
        content = {
            "commit": request.base_commit,
            "targets": sorted(request.targets),
            "placements": placements,
            "routes": payload.get("routes", {}),
            "releases": releases,
            "input_digests": payload.get("input_digests", {}),
        }
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        if not hashlib.sha256(encoded).hexdigest() == digest:
            raise ValueError("reconciliation plan digest does not match queued content")
        self._verify_repository_plan(content)
        profile = placements.get("profile")
        if not isinstance(profile, str) or _NAME.fullmatch(profile) is None:
            raise ValueError("reconciliation profile is invalid")
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
