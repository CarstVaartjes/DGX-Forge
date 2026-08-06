"""Compile Git-independent deployment authority from resolved recipe revisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from .models import LocalRecipeRevision, MaterializedDeployment
from .recipe_contract import recipe_content_sha256, validate_recipe

_ALIAS = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_NODE = re.compile(r"^spk_[0-9a-f]{32}$")


class RecipeDeploymentError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RecipeDeploymentPlan:
    recipe_revision_id: str
    recipe_content_sha256: str
    alias: str
    placements: tuple[dict[str, object], ...]
    placement_digest: str
    plan_digest: str
    authority_digest: str
    base_commit: None
    payload: dict[str, object]


class RecipeDeploymentService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        repository: object | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        # Accepted only to make the authority boundary explicit to callers. It is
        # intentionally never read: recipe operations remain available offline.
        self._legacy_repository = repository

    def preview_recipe(
        self,
        recipe_revision_id: str,
        *,
        alias: str,
        placements: Sequence[Mapping[str, object]],
        actor: str,
    ) -> RecipeDeploymentPlan:
        if _ALIAS.fullmatch(alias) is None or not actor.strip():
            raise RecipeDeploymentError("recipe.deployment_input", "deployment input is invalid")
        with self._sessions() as session:
            revision = session.get(LocalRecipeRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.lifecycle != "resolved" or revision.content_sha256 is None:
                raise RecipeDeploymentError(
                    "recipe.unresolved", "only resolved recipe revisions can deploy"
                )
            document = copy.deepcopy(revision.document)
        validate_recipe(document)
        if recipe_content_sha256(document) != revision.content_sha256:
            raise RecipeDeploymentError(
                "recipe.digest_mismatch", "resolved recipe content changed"
            )
        normalized = _placements(document, placements)
        placement_digest = _digest(normalized)
        payload: dict[str, object] = {
            "schema_version": 1,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_sha256,
            "alias": alias,
            "placements": list(normalized),
            "runtime": copy.deepcopy(document["runtime"]),
            "artifacts": copy.deepcopy(document["artifacts"]),
            "resources": copy.deepcopy(document["resources"]),
            "topology": copy.deepcopy(document["topology"]),
            "endpoint": copy.deepcopy(document["endpoint"]),
            "security": copy.deepcopy(document["security"]),
            "placement_digest": placement_digest,
        }
        plan_digest = _digest(payload)
        authority_digest = _digest(
            {
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": revision.content_sha256,
                "placement_digest": placement_digest,
                "plan_digest": plan_digest,
            }
        )
        return RecipeDeploymentPlan(
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_sha256,
            alias=alias,
            placements=normalized,
            placement_digest=placement_digest,
            plan_digest=plan_digest,
            authority_digest=authority_digest,
            base_commit=None,
            payload=payload,
        )

    def materialize(self, plan: RecipeDeploymentPlan, *, actor: str) -> str:
        now = self._clock()
        deployment = MaterializedDeployment(
            recipe_revision_id=plan.recipe_revision_id,
            alias=plan.alias,
            state="planned",
            placement_digest=plan.placement_digest,
            config={
                "schema_version": 1,
                "plan_digest": plan.plan_digest,
                "authority_digest": plan.authority_digest,
                "payload": copy.deepcopy(plan.payload),
            },
            created_by=actor.strip(),
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            current = session.get(LocalRecipeRevision, plan.recipe_revision_id)
            if (
                current is None
                or current.lifecycle != "resolved"
                or current.content_sha256 != plan.recipe_content_sha256
            ):
                raise RecipeDeploymentError(
                    "recipe.stale_plan", "recipe deployment plan is stale"
                )
            session.add(deployment)
        return deployment.id

    def agent_payloads(
        self, plan: RecipeDeploymentPlan, *, operation_fence: str
    ) -> tuple[dict[str, object], ...]:
        if re.fullmatch(r"[0-9a-f]{64}", operation_fence) is None:
            raise RecipeDeploymentError(
                "recipe.operation_fence", "operation fence is invalid"
            )
        shared = {
            "schema_version": 1,
            "operation_fence": operation_fence,
            "recipe_revision_id": plan.recipe_revision_id,
            "recipe_content_sha256": plan.recipe_content_sha256,
            "plan_digest": plan.plan_digest,
            "placement_digest": plan.placement_digest,
            "runtime": copy.deepcopy(plan.payload["runtime"]),
            "artifacts": copy.deepcopy(plan.payload["artifacts"]),
            "endpoint": copy.deepcopy(plan.payload["endpoint"]),
            "security": copy.deepcopy(plan.payload["security"]),
        }
        return tuple(
            {
                **copy.deepcopy(shared),
                "node_id": placement["node_id"],
                "rank": placement["rank"],
                "role": placement["role"],
            }
            for placement in plan.placements
        )


def _placements(
    document: Mapping[str, object], values: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], ...]:
    normalized: list[dict[str, object]] = []
    for value in values:
        if set(value) != {"node_id", "rank", "role"}:
            raise RecipeDeploymentError("recipe.placement", "placement fields are invalid")
        node_id, rank, role = value["node_id"], value["rank"], value["role"]
        if (
            not isinstance(node_id, str)
            or _NODE.fullmatch(node_id) is None
            or not isinstance(rank, int)
            or isinstance(rank, bool)
            or rank < 0
            or role not in {"entrypoint", "worker"}
        ):
            raise RecipeDeploymentError("recipe.placement", "placement value is invalid")
        normalized.append({"node_id": node_id, "rank": rank, "role": role})
    normalized.sort(key=lambda item: int(item["rank"]))
    ranks = [item["rank"] for item in normalized]
    nodes = [item["node_id"] for item in normalized]
    topology = document["topology"]
    assert isinstance(topology, Mapping)
    if (
        not normalized
        or ranks != list(range(len(normalized)))
        or len(nodes) != len(set(nodes))
        or sum(item["role"] == "entrypoint" for item in normalized) != 1
        or not int(topology["min_nodes"]) <= len(normalized) <= int(topology["max_nodes"])
    ):
        raise RecipeDeploymentError("recipe.topology", "placement does not match topology")
    return tuple(normalized)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
