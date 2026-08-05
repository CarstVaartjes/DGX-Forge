"""Commit-pinned Hermes deployments derived from validated published routes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from .git_content import read_commit_file
from .hermes_policy import HermesAgentPolicy
from .litellm import LiteLlmDeployment
from .route_runtime import PublishedRoute

_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_WORKLOAD = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")


class RepositoryHermesRoutePolicy:
    """Select Hermes' ordered local model group from one immutable commit."""

    def __init__(
        self,
        repository_root: Path,
        *,
        repository_reader: Callable[[str, str], bytes] | None = None,
    ) -> None:
        self._repository_root = repository_root.resolve()
        self._repository_reader = repository_reader or (
            lambda commit, path: read_commit_file(
                self._repository_root,
                commit,
                path,
            )
        )

    def _policy(
        self,
        commit: str,
    ) -> tuple[HermesAgentPolicy, Mapping[str, str]]:
        if _COMMIT.fullmatch(commit) is None:
            raise ValueError("Hermes repository commit is invalid")
        try:
            report = json.loads(
                self._repository_reader(
                    commit,
                    "inventory/reports/model-definitions.json",
                )
            )
            definitions = report["definitions"]
            if not isinstance(definitions, list) or not definitions:
                raise TypeError
            maturity: dict[str, str] = {}
            for row in definitions:
                if not isinstance(row, Mapping):
                    raise TypeError
                workload = row.get("id")
                state = row.get("maturity")
                if (
                    not isinstance(workload, str)
                    or _WORKLOAD.fullmatch(workload) is None
                    or not isinstance(state, str)
                    or workload in maturity
                ):
                    raise TypeError
                maturity[workload] = state
            policy = HermesAgentPolicy.parse(
                self._repository_reader(
                    commit,
                    "config/hermes-agent-policy.toml",
                ),
                known_workloads=frozenset(maturity),
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Hermes repository policy is invalid") from error
        return policy, maturity

    def deployments(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> tuple[LiteLlmDeployment, ...]:
        policy, maturity = self._policy(commit)
        by_workload: dict[str, PublishedRoute] = {}
        for route in routes:
            if not isinstance(route, PublishedRoute):
                raise TypeError("Hermes published route is invalid")
            if route.workload_id in by_workload:
                raise ValueError("Hermes candidate workload route is ambiguous")
            by_workload[route.workload_id] = route
        eligible = policy.eligible(frozenset(by_workload), maturity)
        return tuple(
            LiteLlmDeployment(
                model_name=policy.alias,
                workload=candidate.workload,
                api_base=by_workload[candidate.workload].api_base,
                priority=candidate.priority,
                requests_per_minute=(
                    by_workload[candidate.workload].requests_per_minute
                ),
                tokens_per_minute=(
                    by_workload[candidate.workload].tokens_per_minute
                ),
            )
            for candidate in eligible
        )
