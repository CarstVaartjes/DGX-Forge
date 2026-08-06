"""Production projections for the generic workload package API.

The package routes are intentionally thin.  This module is the production
adapter that binds them to the existing Git repository and operational
database; it does not add a second queue, reconciler, or trust root.  Read
projections are available in the API process.  Mutations that require a
durable workload signer/validation runner remain fail-closed until those
existing worker-owned capabilities are supplied to this adapter.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from spark_profiles.workload_packages import PackageFamily, WorkloadDeployment

from .models import (
    AgentNode,
    Observation,
    PackageCandidate,
    PackageObservation,
    PackageResolution,
    PackageRollout,
    PackageRolloutNode,
    PackageValidationRun,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _raw_digest(value: object) -> str | None:
    if isinstance(value, str):
        return value.removeprefix("sha256:")
    return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _bounded_mapping(value: object, *, maximum: int = 64) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, item in list(value.items())[:maximum]:
        if not isinstance(key, str):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        elif isinstance(item, Mapping):
            result[key] = _bounded_mapping(item, maximum=16)
    return result


class ProductionPackageProjectionService:
    """Bind W11/W15/W16 API projections to Git and SQL state.

    ``repository`` is the same immutable ``RepositoryService`` used by
    desired-state reconciliation.  ``sessions`` is the existing control DB
    session factory.  The service deliberately does not create an alternate
    worker or transport: mutating methods fail closed unless a future caller
    injects the corresponding worker-owned operation boundaries.
    """

    def __init__(
        self,
        repository: Any,
        sessions: sessionmaker[Session],
        *,
        fleet: Callable[[], Mapping[str, object]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(repository, "head", None)):
            raise TypeError("package repository is required")
        self._repository = repository
        self._sessions = sessions
        self._fleet = fleet or (lambda: {"nodes": []})
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idempotency_lock = threading.RLock()
        self._idempotent: dict[tuple[object, ...], Mapping[str, object]] = {}

    # ---- Git-backed definitions -------------------------------------------------

    def _snapshot(self):
        return self._repository.inspect(self._repository.head())

    def _families(self) -> dict[str, PackageFamily]:
        snapshot = self._snapshot()
        families: dict[str, PackageFamily] = {}
        for path in snapshot.documents:
            if not path.startswith("config/package-families/") or not path.endswith(
                ".toml"
            ):
                continue
            document = self._repository.read_document(snapshot.commit, path)
            if isinstance(document.parsed, Mapping):
                family = PackageFamily.load(document.parsed)
                families[family.family_id] = family
        return families

    def families(self, cursor: str | None, limit: int) -> Mapping[str, object]:
        del cursor
        values = []
        for family in sorted(self._families().values(), key=lambda item: item.family_id):
            channels = family.versions.get("channels", ())
            values.append(
                {
                    "id": family.family_id,
                    "promotion_mode": family.promotion.mode,
                    "channels": [item for item in channels if isinstance(item, str)][
                        :64
                    ],
                }
            )
        values = values[:limit]
        return {"families": values, "next_cursor": None, "total": len(values)}

    # ---- SQL-backed candidate/resolution projections ---------------------------

    @staticmethod
    def _candidate_row(session: Session, candidate_id: str) -> PackageCandidate:
        row = session.get(PackageCandidate, candidate_id)
        if row is None:
            raise KeyError(candidate_id)
        return row

    @staticmethod
    def _candidate_value(row: PackageCandidate) -> dict[str, object]:
        summary = _bounded_mapping(row.summary)
        release = summary.get("release")
        release_value: dict[str, object] | None = None
        if isinstance(release, Mapping):
            release_value = {}
            for key in ("release_digest", "lock_digest"):
                value = release.get(key)
                if isinstance(value, str):
                    release_value[key] = value[:128]
            components = release.get("components")
            if isinstance(components, list):
                release_value["components"] = [
                    {
                        "name": item.get("name"),
                        "digest": item.get("digest"),
                        "kind": item.get("kind"),
                    }
                    for item in components[:128]
                    if isinstance(item, Mapping)
                ]
            dependencies = release.get("dependencies")
            if isinstance(dependencies, list):
                release_value["dependencies"] = [
                    item for item in dependencies[:256] if isinstance(item, str)
                ]
            provenance = release.get("provenance")
            if isinstance(provenance, list):
                release_value["provenance"] = [
                    {"kind": item.get("kind"), "digest": item.get("digest")}
                    for item in provenance[:128]
                    if isinstance(item, Mapping)
                ]
            if not release_value:
                release_value = None
        return {
            "id": row.id,
            "family_id": row.family_id,
            "release_key": str(summary.get("release_key", row.source_reference)),
            "upstream_version": row.upstream_version,
            "state": row.state,
            "reason_code": row.reason_code,
            "metadata": _bounded_mapping(summary.get("metadata")),
            "release": release_value,
        }

    def candidates(
        self, family_id: str | None, cursor: str | None, limit: int
    ) -> Mapping[str, object]:
        del cursor
        with self._sessions() as session:
            statement = select(PackageCandidate).order_by(PackageCandidate.id)
            if family_id is not None:
                statement = statement.where(PackageCandidate.family_id == family_id)
            rows = list(session.scalars(statement).fetchmany(limit))
        values = [self._candidate_value(row) for row in rows]
        return {"candidates": values, "next_cursor": None, "total": len(values)}

    def candidate(self, candidate_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            return self._candidate_value(self._candidate_row(session, candidate_id))

    def _resolution_row(self, session: Session, candidate_id: str) -> PackageResolution:
        self._candidate_row(session, candidate_id)
        row = session.scalar(
            select(PackageResolution)
            .where(PackageResolution.candidate_id == candidate_id)
            .order_by(PackageResolution.updated_at.desc(), PackageResolution.id.desc())
        )
        if row is None:
            raise KeyError(candidate_id)
        return row

    def resolution(self, candidate_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            row = self._resolution_row(session, candidate_id)
        if row.release_digest is None:
            # The wire response intentionally binds a release only for a
            # resolved row.  Let the route boundary turn pending/unsupported
            # rows into its bounded 422 response rather than emitting a
            # schema-invalid null digest.
            raise ValueError("candidate resolution has no release")
        return {
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + row.release_digest,
            "state": row.state,
        }

    def compatibility(self, candidate_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            row = self._resolution_row(session, candidate_id)
        summary = _bounded_mapping(row.summary)
        raw_nodes = summary.get("compatible_node_ids", ())
        nodes = [item for item in raw_nodes if isinstance(item, str)] if isinstance(raw_nodes, list) else []
        digest = summary.get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            digest = "sha256:" + _digest(summary)
        release = row.release_digest
        if release is None:
            raise ValueError("candidate resolution has no release")
        return {
            "candidate_id": candidate_id,
            "release_digest": "sha256:" + release,
            "digest": digest,
            "compatible_node_ids": nodes[:512],
        }

    # ---- Durable status projections --------------------------------------------

    @staticmethod
    def _progress(value: object) -> dict[str, int]:
        raw = value if isinstance(value, Mapping) else {}
        result: dict[str, int] = {}
        for key in ("completed", "failed", "running", "total"):
            item = raw.get(key, 0)
            result[key] = item if isinstance(item, int) and item >= 0 else 0
        return result

    def validation_status(self, validation_id: str) -> Mapping[str, object]:
        with self._sessions() as session:
            row = session.get(PackageValidationRun, validation_id)
        if row is None:
            raise KeyError(validation_id)
        progress = self._progress(row.progress)
        if progress["total"] == 0:
            progress["total"] = 1
        return {
            "id": row.id,
            "state": row.state,
            "plan_digest": "sha256:" + _digest(
                {"candidate_id": row.candidate_id, "release_digest": row.release_digest}
            ),
            "progress": progress,
            "failure": row.reason_code,
            "job_id": None,
            "audit_request_id": None,
            "nodes": [],
            "rollback_rollout_id": None,
            "rollback_selector": None,
        }

    # ---- Repository deployment projections -------------------------------------

    def _deployments(self) -> dict[str, WorkloadDeployment]:
        snapshot = self._snapshot()
        result: dict[str, WorkloadDeployment] = {}
        for path in snapshot.documents:
            if not path.startswith("config/workload-deployments/") or not path.endswith(
                ".toml"
            ):
                continue
            document = self._repository.read_document(snapshot.commit, path)
            if isinstance(document.parsed, Mapping):
                deployment = WorkloadDeployment.load(document.parsed)
                result[deployment.deployment_id] = deployment
        return result

    def deployments(self, cursor: str | None, limit: int) -> Mapping[str, object]:
        del cursor
        snapshot = self._snapshot()
        with self._sessions() as session:
            rollouts = list(
                session.scalars(
                    select(PackageRollout).order_by(
                        PackageRollout.created_at.desc(), PackageRollout.id.desc()
                    )
                )
            )
        latest_rollout: dict[str, str] = {}
        for rollout in rollouts:
            latest_rollout.setdefault(rollout.deployment_id, rollout.id)
        values = []
        for deployment in sorted(self._deployments().values(), key=lambda item: item.deployment_id):
            release_path = (
                "manifests/workload-releases/"
                f"{deployment.family_id}/{deployment.release_digest}.json"
            )
            state = "approved" if release_path in snapshot.documents else "unapproved"
            values.append(
                {
                    "id": deployment.deployment_id,
                    "family_id": deployment.family_id,
                    "release_digest": "sha256:" + deployment.release_digest,
                    "previous_release_digest": None,
                    "state": state,
                    "rollout_id": latest_rollout.get(deployment.deployment_id),
                }
            )
        values = values[:limit]
        return {"deployments": values, "next_cursor": None, "total": len(values)}

    def deployment(self, deployment_id: str) -> Mapping[str, object]:
        values = self.deployments(None, 10_000)["deployments"]
        for value in values:
            if isinstance(value, Mapping) and value.get("id") == deployment_id:
                return value
        raise KeyError(deployment_id)

    def rollout_status(
        self, deployment_id: str, rollout_id: str, cursor: str | None, limit: int
    ) -> Mapping[str, object]:
        del cursor
        with self._sessions() as session:
            rollout = session.get(PackageRollout, rollout_id)
            if rollout is None or rollout.deployment_id != deployment_id:
                raise KeyError(rollout_id)
            nodes = list(
                session.scalars(
                    select(PackageRolloutNode)
                    .where(PackageRolloutNode.rollout_id == rollout.id)
                    .order_by(PackageRolloutNode.batch_index, PackageRolloutNode.node_order)
                )
            )[:limit]
        progress = self._progress(rollout.progress)
        progress["total"] = max(progress["total"], len(nodes))
        progress["completed"] = max(
            progress["completed"], sum(node.state == "accepted" for node in nodes)
        )
        return {
            "id": rollout.id,
            "state": rollout.state,
            "plan_digest": "sha256:" + rollout.plan_digest,
            "progress": progress,
            "failure": rollout.failure_reason,
            "job_id": rollout.job_id,
            "audit_request_id": None,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "state": node.state,
                    "batch_index": node.batch_index,
                    "completed": 1 if node.state == "accepted" else 0,
                    "total": 1,
                }
                for node in nodes
            ],
            "rollback_rollout_id": None,
            "rollback_selector": "retained",
        }

    # ---- Agent-observed per-Spark inventory ------------------------------------

    @staticmethod
    def _health_resources(payload: object) -> tuple[dict[str, int], dict[str, int]]:
        value = payload if isinstance(payload, Mapping) else {}
        storage = value.get("storage") if isinstance(value.get("storage"), Mapping) else value
        resources = value.get("resources") if isinstance(value.get("resources"), Mapping) else value

        def integer(mapping: object, *names: str) -> int:
            if not isinstance(mapping, Mapping):
                return 0
            for name in names:
                item = mapping.get(name)
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                    return item
            return 0

        total = integer(storage, "total_bytes", "disk_total_bytes", "storage_total_bytes")
        free = integer(storage, "free_bytes", "disk_available_bytes", "storage_available_bytes")
        return (
            {
                "total_bytes": total,
                "used_bytes": max(0, total - free) if total else 0,
                "free_bytes": free,
                "reserved_bytes": integer(storage, "reserved_bytes"),
                "reclaimable_bytes": integer(storage, "reclaimable_bytes"),
            },
            {
                "host_memory_total_bytes": integer(resources, "host_memory_total_bytes", "memory_total_bytes"),
                "host_memory_free_bytes": integer(resources, "host_memory_free_bytes", "memory_available_bytes"),
                "gpu_memory_total_bytes": integer(resources, "gpu_memory_total_bytes"),
                "gpu_memory_free_bytes": integer(resources, "gpu_memory_free_bytes"),
                "gpu_count": integer(resources, "gpu_count"),
            },
        )

    def inventory(
        self,
        node_id: str | None,
        deployment_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, object]:
        del cursor
        with self._sessions() as session:
            nodes = list(session.scalars(select(AgentNode).order_by(AgentNode.node_id)))
            health_rows = list(
                session.scalars(
                    select(Observation)
                    .where(Observation.kind == "health")
                    .order_by(Observation.observed_at.desc())
                )
            )
            package_rows = list(
                session.scalars(select(PackageObservation).order_by(PackageObservation.node_id))
            )
        latest_health: dict[str, Observation] = {}
        for row in health_rows:
            latest_health.setdefault(row.node_id, row)
        grouped: dict[str, list[PackageObservation]] = {}
        for row in package_rows:
            if deployment_id is None or row.deployment_id == deployment_id:
                grouped.setdefault(row.node_id, []).append(row)
        values = []
        for node in nodes:
            if node_id is not None and node.node_id != node_id:
                continue
            storage, resources = self._health_resources(
                latest_health.get(node.node_id).payload
                if node.node_id in latest_health
                else {}
            )
            packages = []
            current_generation = None
            for row in grouped.get(node.node_id, [])[:2048]:
                summary = _bounded_mapping(row.summary)
                if row.state in {"active", "healthy"}:
                    current_generation = "sha256:" + row.release_digest
                package_state = {
                    "active": "active",
                    "healthy": "active",
                    "stopped": "retained",
                    "prepared": "staged",
                    "failed": "failed",
                }.get(row.state, "available")
                envelope = _bounded_mapping(summary.get("resources"))
                envelope.setdefault("download_bytes", 0)
                envelope.setdefault("installed_bytes", 0)
                envelope.setdefault("transient_bytes", 0)
                envelope.setdefault("host_memory_bytes", 0)
                envelope.setdefault("gpu_memory_bytes", 0)
                envelope.setdefault("kv_cache_base_bytes", 0)
                envelope.setdefault("kv_cache_per_token_bytes", 0)
                envelope.setdefault("required_sparks", 1)
                envelope.setdefault("topology", "single")
                packages.append(
                    {
                        "deployment_id": row.deployment_id,
                        "family_id": summary.get("family_id"),
                        "release_digest": "sha256:" + row.release_digest,
                        "content_group": str(summary.get("content_group", "workload")),
                        "state": package_state,
                        "bytes_total": int(summary.get("bytes_total", 0) or 0),
                        "bytes_complete": int(summary.get("bytes_complete", 0) or 0),
                        "bytes_remaining": int(summary.get("bytes_remaining", 0) or 0),
                        "installed_bytes": int(summary.get("installed_bytes", 0) or 0),
                        "reclaimable_bytes": int(summary.get("reclaimable_bytes", 0) or 0),
                        "reserved_bytes": int(summary.get("reserved_bytes", 0) or 0),
                        "active": row.state in {"active", "healthy"},
                        "retained": row.state == "stopped",
                        "leased": False,
                        "operation_id": row.operation_id,
                        "last_operation_state": row.state,
                        "last_operation_error": None,
                        "resources": envelope,
                    }
                )
            values.append(
                {
                    "node_id": node.node_id,
                    "online": node.state == "active" and node.revoked_at is None,
                    "observed_at": _iso(latest_health.get(node.node_id).observed_at)
                    if node.node_id in latest_health
                    else None,
                    "storage": storage,
                    "resources": resources,
                    "current_generation": current_generation,
                    "packages": packages,
                }
            )
        values = values[:limit]
        return {"nodes": values, "next_cursor": None, "total": len(values)}

    def removal_preview(
        self, deployment_id: str, release_digest: str, node_ids: tuple[str, ...]
    ) -> Mapping[str, object]:
        wanted = set(node_ids)
        inventory = self.inventory(None, deployment_id, None, 512)
        rows = []
        for node in inventory.get("nodes", []):
            if not isinstance(node, Mapping) or node.get("node_id") not in wanted:
                continue
            packages = node.get("packages", [])
            package = next(
                (
                    item
                    for item in packages
                    if isinstance(item, Mapping)
                    and item.get("release_digest") == release_digest
                ),
                None,
            )
            active = bool(package and package.get("active"))
            leased = bool(package and package.get("leased"))
            retained = bool(package and package.get("retained"))
            blocked = "active" if active else "leased" if leased else "retained" if retained else None
            rows.append(
                {
                    "node_id": node.get("node_id"),
                    "state": "blocked" if blocked else "removable",
                    "active": active,
                    "retained": retained,
                    "leased": leased,
                    "reclaimable_bytes": int(package.get("reclaimable_bytes", 0)) if package else 0,
                    "dependencies": [],
                    "blocked_reason": blocked,
                }
            )
        digest = "sha256:" + _digest(
            {"deployment_id": deployment_id, "release_digest": release_digest, "nodes": rows}
        )
        return {
            "digest": digest,
            "state": "blocked" if any(item["blocked_reason"] for item in rows) else "ready",
            "deployment_id": deployment_id,
            "release_digest": release_digest,
            "nodes": rows,
            "reclaimable_bytes": sum(int(item["reclaimable_bytes"]) for item in rows),
            "blocked_nodes": [item["node_id"] for item in rows if item["blocked_reason"]],
        }

    # ---- Explicitly fail-closed mutation boundary ------------------------------

    @staticmethod
    def _mutation_unavailable(*_args: object, **_kwargs: object) -> Mapping[str, object]:
        raise RuntimeError("durable workload mutation service is not installed")

    validation_preview = _mutation_unavailable
    validate = _mutation_unavailable
    promotion_preview = _mutation_unavailable
    promote = _mutation_unavailable
    rollout_preview = _mutation_unavailable
    rollout = _mutation_unavailable
    rollback_preview = _mutation_unavailable
    rollback = _mutation_unavailable
    repair_preview = _mutation_unavailable
    repair = _mutation_unavailable
    gc_preview = _mutation_unavailable
    gc = _mutation_unavailable
    remove = _mutation_unavailable

    def idempotency(
        self,
        actor: str,
        request_id: str,
        fingerprint: tuple[object, ...],
        call: Callable[[], Mapping[str, object]],
    ) -> tuple[Mapping[str, object], bool]:
        key = (actor, request_id, *fingerprint)
        with self._idempotency_lock:
            if key in self._idempotent:
                return self._idempotent[key], True
            result = dict(call())
            self._idempotent[key] = result
            return result, False


__all__ = ["ProductionPackageProjectionService"]
