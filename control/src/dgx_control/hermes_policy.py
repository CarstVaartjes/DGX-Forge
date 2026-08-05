"""Strict repository policy for Hermes' local-only model group."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass


_WORKLOAD = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_POLICY_FIELDS = frozenset({"schema_version", "alias", "local_only", "candidates"})
_CANDIDATE_FIELDS = frozenset({"workload", "priority", "minimum_maturity"})


class HermesPolicyError(ValueError):
    """The commit-pinned Hermes policy is invalid or not local-only."""


@dataclass(frozen=True)
class HermesCandidate:
    workload: str
    priority: int
    minimum_maturity: str


@dataclass(frozen=True)
class HermesAgentPolicy:
    schema_version: int
    alias: str
    local_only: bool
    candidates: tuple[HermesCandidate, ...]

    @classmethod
    def parse(
        cls,
        content: bytes,
        *,
        known_workloads: AbstractSet[str],
    ) -> HermesAgentPolicy:
        try:
            document = tomllib.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise HermesPolicyError("Hermes policy is invalid TOML") from error
        if set(document) != _POLICY_FIELDS:
            raise HermesPolicyError("Hermes policy fields are invalid")
        if (
            document["schema_version"] != 1
            or document["alias"] != "hermes-agent"
            or document["local_only"] is not True
        ):
            raise HermesPolicyError(
                "Hermes policy must be version one, use hermes-agent, and be local-only"
            )
        rows = document["candidates"]
        if not isinstance(rows, list) or not rows:
            raise HermesPolicyError("Hermes policy candidates are required")

        candidates: list[HermesCandidate] = []
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != _CANDIDATE_FIELDS:
                raise HermesPolicyError("Hermes candidate fields are invalid")
            workload = row["workload"]
            priority = row["priority"]
            minimum_maturity = row["minimum_maturity"]
            if (
                not isinstance(workload, str)
                or _WORKLOAD.fullmatch(workload) is None
                or workload not in known_workloads
            ):
                raise HermesPolicyError("Hermes candidate workload is not known")
            if (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or priority < 1
            ):
                raise HermesPolicyError("Hermes candidate priority must be positive")
            if minimum_maturity != "accepted":
                raise HermesPolicyError("Hermes candidate maturity must be accepted")
            candidates.append(
                HermesCandidate(
                    workload=workload,
                    priority=priority,
                    minimum_maturity=minimum_maturity,
                )
            )

        if (
            len({candidate.workload for candidate in candidates}) != len(candidates)
            or len({candidate.priority for candidate in candidates}) != len(candidates)
        ):
            raise HermesPolicyError(
                "Hermes candidates must have unique workloads and priorities"
            )
        return cls(
            schema_version=1,
            alias="hermes-agent",
            local_only=True,
            candidates=tuple(sorted(candidates, key=lambda candidate: candidate.priority)),
        )

    def eligible(
        self,
        active_workloads: AbstractSet[str],
        maturity: Mapping[str, str],
    ) -> tuple[HermesCandidate, ...]:
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.workload in active_workloads
            and maturity.get(candidate.workload) == candidate.minimum_maturity
        )
