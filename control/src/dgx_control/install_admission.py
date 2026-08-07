"""Explainable, reservation-aware disk admission for recipe installation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .artifact_sizes import ArtifactSizeError, ArtifactSizeResolver
from .inventory_repository import InventoryRepository
from .models import (
    AgentNode,
    InstallationNode,
    LocalRecipeRevision,
    NodeArtifact,
    RecipeInstallation,
    ResourceReservation,
)


@dataclass(frozen=True, slots=True)
class AdmissionReason:
    code: str; detail: str


@dataclass(frozen=True, slots=True)
class InstallNodePlan:
    node_id: str; allowed: bool; inventory_observed_at: datetime | None; free_bytes: int | None
    active_reserved_bytes: int; reused_bytes: int; required_download_bytes: int; required_bytes: int; disk_floor_bytes: int; free_after_bytes: int | None
    blockers: tuple[AdmissionReason, ...]; warnings: tuple[AdmissionReason, ...]


@dataclass(frozen=True, slots=True)
class InstallPlan:
    recipe_revision_id: str; recipe_content_sha256: str; allowed: bool; nodes: tuple[InstallNodePlan, ...]; plan_digest: str


class InstallPlanConflict(RuntimeError): pass


class InstallAdmissionService:
    def __init__(self, sessions: sessionmaker[Session], *, sizes: ArtifactSizeResolver, inventory_max_age: int = 300, disk_floor_bytes: int = 10_000_000_000) -> None:
        self._sessions, self._sizes, self._inventory = sessions, sizes, InventoryRepository(sessions)
        self._inventory_max_age, self._disk_floor = inventory_max_age, disk_floor_bytes

    def plan_install(self, recipe_revision_id: str, node_ids: tuple[str, ...], *, now: datetime) -> InstallPlan:
        if not node_ids or len(node_ids) != len(set(node_ids)): raise ValueError("installation nodes are invalid")
        with self._sessions() as session:
            revision = session.get(LocalRecipeRevision, recipe_revision_id)
            if revision is None: raise KeyError(recipe_revision_id)
            if revision.lifecycle != "resolved" or revision.content_sha256 is None: raise ValueError("recipe revision is not resolved")
            document = revision.document
        topology = document.get("topology"); resources = document.get("resources")
        if not isinstance(topology, dict) or not int(topology["min_nodes"]) <= len(node_ids) <= int(topology["max_nodes"]): raise ValueError("installation placement violates recipe topology")
        if not isinstance(resources, dict) or not isinstance(resources.get("per_node"), dict): raise TypeError("recipe resources are invalid")
        per_node = resources["per_node"]; installed = int(per_node["installed_bytes"]); staging = int(per_node["staging_bytes"]); download = int(per_node["download_bytes"])
        try: artifacts = self._sizes.resolve(document)
        except ArtifactSizeError:
            artifacts = ()
        plans: list[InstallNodePlan] = []
        for node_id in node_ids:
            blockers: list[AdmissionReason] = []; warnings: list[AdmissionReason] = []
            if not artifacts: blockers.append(AdmissionReason("install.unknown_artifact_size", "Image or model size metadata is incomplete."))
            try: snapshot = self._inventory.latest(node_id, now=now, maximum_age=self._inventory_max_age)
            except KeyError:
                snapshot = None; blockers.append(AdmissionReason("install.inventory_missing", "No authenticated inventory is available for this Spark."))
            if snapshot is not None and snapshot.stale: blockers.append(AdmissionReason("install.stale_inventory", "Spark disk inventory is stale; refresh it before installing."))
            if snapshot is not None and snapshot.artifact_store_read_only: blockers.append(AdmissionReason("install.artifact_store_read_only", "The Spark artifact store is read-only."))
            with self._sessions() as session:
                present = {row.source: row.size_bytes for row in session.scalars(select(NodeArtifact).where(NodeArtifact.node_id == node_id, NodeArtifact.state == "verified"))}
                reserved = int(session.scalar(select(func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)).where(ResourceReservation.node_id == node_id, ResourceReservation.kind == "disk", ResourceReservation.state == "active")) or 0)
            reused = min(installed, sum(size for source, size in present.items() if any(item.source == source and item.size_bytes == size for item in artifacts)))
            required = max(0, installed - reused) + staging; required_download = max(0, download - reused)
            free = snapshot.disk_free_bytes if snapshot else None; free_after = None if free is None else free - reserved - required
            if free_after is not None and free_after < self._disk_floor: blockers.append(AdmissionReason("install.insufficient_disk", f"Installation would leave {free_after} bytes, below the required {self._disk_floor}-byte floor."))
            plans.append(InstallNodePlan(node_id, not blockers, snapshot.observed_at if snapshot else None, free, reserved, reused, required_download, required, self._disk_floor, free_after, tuple(blockers), tuple(warnings)))
        identity = {"schema_version": 1, "recipe_revision_id": recipe_revision_id, "recipe_content_sha256": revision.content_sha256, "nodes": [{**asdict(plan), "inventory_observed_at": plan.inventory_observed_at.isoformat() if plan.inventory_observed_at else None} for plan in plans]}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return InstallPlan(recipe_revision_id, revision.content_sha256, all(plan.allowed for plan in plans), tuple(plans), digest)

    def accept_install(self, plan: InstallPlan, *, actor: str, now: datetime) -> str:
        with self._sessions.begin() as session:
            return self.accept_install_in_session(
                session, plan, actor=actor, now=now
            )

    def accept_install_in_session(
        self,
        session: Session,
        plan: InstallPlan,
        *,
        actor: str,
        now: datetime,
    ) -> str:
        fresh = self.plan_install(plan.recipe_revision_id, tuple(node.node_id for node in plan.nodes), now=now)
        if not fresh.allowed or fresh.plan_digest != plan.plan_digest:
            raise InstallPlanConflict("installation plan is stale or blocked")
        plan_document = {"schema_version": 1, "recipe_revision_id": plan.recipe_revision_id, "recipe_content_sha256": plan.recipe_content_sha256, "plan_digest": plan.plan_digest, "nodes": [{**asdict(node), "inventory_observed_at": node.inventory_observed_at.isoformat() if node.inventory_observed_at else None} for node in plan.nodes]}
        installation = RecipeInstallation(recipe_revision_id=plan.recipe_revision_id, plan_digest=plan.plan_digest, plan=plan_document, state="planned", actor=actor, created_at=now, updated_at=now)
        for node in sorted(plan.nodes, key=lambda item: item.node_id):
            if session.scalar(select(AgentNode).where(AgentNode.node_id == node.node_id).with_for_update()) is None: raise InstallPlanConflict("installation node disappeared")
            active = int(session.scalar(select(func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)).where(ResourceReservation.node_id == node.node_id, ResourceReservation.kind == "disk", ResourceReservation.state == "active")) or 0)
            if node.free_bytes is None or node.free_bytes - active - node.required_bytes < self._disk_floor: raise InstallPlanConflict("disk capacity changed while reserving")
        session.add(installation); session.flush()
        for node in plan.nodes:
            session.add(InstallationNode(installation_id=installation.id, node_id=node.node_id, state="planned", required_bytes=node.required_bytes, installed_bytes=0, updated_at=now))
            session.add(ResourceReservation(node_id=node.node_id, kind="disk", resource_key=plan.plan_digest, amount_bytes=node.required_bytes, owner_kind="installation", owner_id=installation.id, state="active", plan_digest=plan.plan_digest, created_at=now))
        return installation.id
