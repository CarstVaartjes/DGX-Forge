"""Typed, secret-free administrative API for workload package control."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditRecord
from .auth import Actor
from .operation_api import bounded_error_responses

_DIGEST = r"^sha256:[0-9a-f]{64}$"
# Discovery implementations historically used content digests as candidate
# IDs, while the durable W11 projection uses UUID primary keys. Accept both
# exact forms at the API boundary; neither form is free-form user input.
_CANDIDATE_ID = r"^(?:[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
_IDENTIFIER = r"^[a-z0-9][a-z0-9._-]{0,126}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|credential|password|secret|token|private.?key|certificate|payload|upload)"
)

PACKAGE_OPERATION_IDS = {
    ("get", "/api/v1/packages/families"): "listPackageFamilies",
    ("get", "/api/v1/packages/candidates"): "listPackageCandidates",
    ("get", "/api/v1/packages/candidates/{candidate_id}"): "getPackageCandidate",
    ("get", "/api/v1/packages/candidates/{candidate_id}/resolution"): "getPackageResolution",
    ("get", "/api/v1/packages/candidates/{candidate_id}/compatibility"): "getPackageCompatibility",
    ("post", "/api/v1/packages/candidates/{candidate_id}/validation-preview"): "previewPackageValidation",
    ("post", "/api/v1/packages/candidates/{candidate_id}/validate"): "validatePackageCandidate",
    ("get", "/api/v1/packages/validations/{validation_id}"): "getPackageValidation",
    ("post", "/api/v1/packages/candidates/{candidate_id}/promotion-preview"): "previewPackagePromotion",
    ("post", "/api/v1/packages/candidates/{candidate_id}/promote"): "promotePackage",
    ("get", "/api/v1/deployments"): "listPackageDeployments",
    ("get", "/api/v1/deployments/{deployment_id}"): "getPackageDeployment",
    ("post", "/api/v1/deployments/{deployment_id}/rollout-preview"): "previewPackageRollout",
    ("post", "/api/v1/deployments/{deployment_id}/rollouts"): "rolloutPackageDeployment",
    ("get", "/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}"): "getPackageRollout",
    ("post", "/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}/rollback-preview"): "previewPackageRollback",
    ("post", "/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}/rollback"): "rollbackPackageDeployment",
    ("post", "/api/v1/deployments/{deployment_id}/repair-preview"): "previewPackageRepair",
    ("post", "/api/v1/deployments/{deployment_id}/repair"): "repairPackageDeployment",
    ("post", "/api/v1/packages/gc-preview"): "previewPackageGc",
    ("post", "/api/v1/packages/gc"): "applyPackageGc",
    ("get", "/api/v1/packages/inventory"): "listPackageInventory",
    ("post", "/api/v1/packages/inventory/remove-preview"): "previewPackageRemoval",
    ("post", "/api/v1/packages/inventory/remove"): "removePackageInventory",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditSink(Protocol):
    def append(self, event: AuditRecord) -> None: ...


class PackageFamilyResponse(StrictModel):
    id: str = Field(pattern=_IDENTIFIER)
    promotion_mode: str = Field(min_length=1, max_length=32)
    channels: list[str] = Field(max_length=64)


class PackageFamiliesResponse(StrictModel):
    families: list[PackageFamilyResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0, le=10_000_000)


class PackageCandidateResponse(StrictModel):
    id: str = Field(pattern=_CANDIDATE_ID)
    family_id: str = Field(pattern=_IDENTIFIER)
    release_key: str = Field(min_length=1, max_length=256)
    upstream_version: str = Field(min_length=1, max_length=256)
    state: str = Field(min_length=1, max_length=64)
    reason_code: str | None = Field(default=None, max_length=80)
    metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, max_length=64
    )
    release: PackageReleaseMetadata | None = None


class PackageComponentResponse(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=_DIGEST)
    kind: str = Field(min_length=1, max_length=64)


class PackageProvenanceResponse(StrictModel):
    kind: str = Field(min_length=1, max_length=64)
    digest: str = Field(pattern=_DIGEST)


class PackageReleaseMetadata(StrictModel):
    release_digest: str = Field(pattern=_DIGEST)
    lock_digest: str = Field(pattern=_DIGEST)
    components: list[PackageComponentResponse] = Field(default_factory=list, max_length=128)
    dependencies: list[str] = Field(default_factory=list, max_length=256)
    provenance: list[PackageProvenanceResponse] = Field(default_factory=list, max_length=128)


class PackageCandidatesResponse(StrictModel):
    candidates: list[PackageCandidateResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0, le=10_000_000)


class PackageResolutionResponse(StrictModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    release_digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)


class PackageCompatibilityResponse(StrictModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    release_digest: str = Field(pattern=_DIGEST)
    digest: str = Field(pattern=_DIGEST)
    compatible_node_ids: list[str] = Field(max_length=512)


class PackageResourceValues(StrictModel):
    download_bytes: int = Field(ge=0)
    installed_bytes: int = Field(ge=0)
    transient_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    host_memory_bytes: int = Field(ge=0)
    gpu_memory_bytes: int = Field(ge=0)
    kv_cache_base_bytes: int = Field(ge=0)
    kv_cache_per_token_bytes: int = Field(ge=0)


class PackageResourceEnvelope(PackageResourceValues):
    """Bounded per-Spark resource requirements from a promoted release."""

    required_sparks: int = Field(ge=1, le=512)
    topology: str = Field(min_length=1, max_length=128)


class PackageRolloutResourceEnvelope(StrictModel):
    """Signed release sizing for one-node and aggregate placement views."""

    schema_version: int = Field(ge=1)
    per_node: PackageResourceValues
    aggregate: PackageResourceValues
    required_sparks: int = Field(ge=1, le=512)
    topology: str = Field(min_length=1, max_length=128)
    measurement: str = Field(min_length=1, max_length=32)
    evidence: list[dict[str, str]] = Field(max_length=16)


class PackagePlanResponse(StrictModel):
    digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)
    candidate_id: str | None = Field(default=None, pattern=_CANDIDATE_ID)
    validation_id: str | None = Field(default=None, pattern=_UUID)
    deployment_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    release_digest: str | None = Field(default=None, pattern=_DIGEST)
    reclaim_bytes: int | None = Field(default=None, ge=0)
    batches: list[list[str]] = Field(default_factory=list, max_length=128)
    canary_node: str | None = Field(default=None, max_length=128)
    offline_pending: list[str] = Field(default_factory=list, max_length=512)
    storage_bytes: int | None = Field(default=None, ge=0)
    download_bytes: int | None = Field(default=None, ge=0)
    resource_envelope: PackageRolloutResourceEnvelope | None = None


class PackagePlanRequest(StrictModel):
    plan_digest: str = Field(pattern=_DIGEST)


class PackagePromotionRequest(StrictModel):
    preview_digest: str = Field(pattern=_DIGEST)


class PackagePromotionResponse(StrictModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    release_digest: str = Field(pattern=_DIGEST)
    digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)


class DeploymentResponse(StrictModel):
    id: str = Field(pattern=_IDENTIFIER)
    family_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    release_digest: str = Field(pattern=_DIGEST)
    previous_release_digest: str | None = Field(default=None, pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)
    rollout_id: str | None = Field(default=None, pattern=_UUID)


class DeploymentsResponse(StrictModel):
    deployments: list[DeploymentResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0, le=10_000_000)


class PackageProgress(StrictModel):
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    running: int = Field(ge=0)
    total: int = Field(ge=0)


class PackageNodeProgress(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=64)
    batch_index: int = Field(ge=0)
    completed: int = Field(ge=0)
    total: int = Field(ge=0)


class PackageProgressResponse(StrictModel):
    id: str = Field(pattern=_UUID)
    state: str = Field(min_length=1, max_length=64)
    plan_digest: str = Field(pattern=_DIGEST)
    progress: PackageProgress
    failure: str | None = Field(default=None, max_length=256)
    job_id: str | None = Field(default=None, min_length=1, max_length=128)
    audit_request_id: str | None = Field(default=None, min_length=1, max_length=64)
    nodes: list[PackageNodeProgress] = Field(default_factory=list, max_length=512)
    rollback_rollout_id: str | None = Field(default=None, pattern=_UUID)
    rollback_selector: str | None = Field(default=None, max_length=128)


class PackageInventoryItem(StrictModel):
    """One release/content group as observed on one Spark."""

    deployment_id: str = Field(pattern=_IDENTIFIER)
    family_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    release_digest: str = Field(pattern=_DIGEST)
    content_group: str = Field(min_length=1, max_length=128)
    state: str = Field(
        pattern=r"^(downloading|staged|available|active|retained|leased|removable|failed)$"
    )
    bytes_total: int = Field(ge=0)
    bytes_complete: int = Field(ge=0)
    bytes_remaining: int = Field(ge=0)
    installed_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    active: bool
    retained: bool
    leased: bool
    operation_id: str | None = Field(default=None, max_length=128)
    last_operation_state: str | None = Field(default=None, max_length=64)
    last_operation_error: str | None = Field(default=None, max_length=256)
    resources: PackageResourceEnvelope


class PackageSparkStorage(StrictModel):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    reserved_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)


class PackageSparkResources(StrictModel):
    host_memory_total_bytes: int = Field(ge=0)
    host_memory_free_bytes: int = Field(ge=0)
    gpu_memory_total_bytes: int = Field(ge=0)
    gpu_memory_free_bytes: int = Field(ge=0)
    gpu_count: int = Field(ge=0, le=512)


class PackageSparkInventory(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    online: bool
    observed_at: str | None = Field(default=None, max_length=64)
    storage: PackageSparkStorage
    resources: PackageSparkResources
    current_generation: str | None = Field(default=None, pattern=_DIGEST)
    packages: list[PackageInventoryItem] = Field(default_factory=list, max_length=2048)


class PackageInventoryResponse(StrictModel):
    nodes: list[PackageSparkInventory] = Field(max_length=512)
    next_cursor: str | None = Field(default=None, max_length=512)
    total: int = Field(ge=0, le=10_000_000)


class PackageRemovalRequest(StrictModel):
    deployment_id: str = Field(pattern=_IDENTIFIER)
    release_digest: str = Field(pattern=_DIGEST)
    node_ids: list[str] = Field(min_length=1, max_length=512)


class PackageRemovalNode(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=64)
    active: bool
    retained: bool
    leased: bool
    reclaimable_bytes: int = Field(ge=0)
    dependencies: list[str] = Field(default_factory=list, max_length=256)
    blocked_reason: str | None = Field(default=None, max_length=256)


class PackageRemovalPreviewResponse(StrictModel):
    digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)
    deployment_id: str = Field(pattern=_IDENTIFIER)
    release_digest: str = Field(pattern=_DIGEST)
    nodes: list[PackageRemovalNode] = Field(max_length=512)
    reclaimable_bytes: int = Field(ge=0)
    blocked_nodes: list[str] = Field(default_factory=list, max_length=512)


def _safe_metadata(value: object) -> dict[str, str | int | float | bool | None]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, str | int | float | bool | None] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _SENSITIVE_KEY.search(key):
            continue
        if isinstance(item, str):
            safe[key] = item[:256]
        elif isinstance(item, (int, float, bool)) or item is None:
            safe[key] = item
        if len(safe) == 64:
            break
    return safe


def _candidate_document(value: Mapping[str, object]) -> dict[str, object]:
    release = value.get("release")
    return {
        "id": value.get("id"),
        "family_id": value.get("family_id"),
        "release_key": value.get("release_key"),
        "upstream_version": value.get("upstream_version"),
        "state": value.get("state"),
        "reason_code": value.get("reason_code"),
        "metadata": _safe_metadata(value.get("metadata")),
        "release": _release_document(release) if isinstance(release, Mapping) else None,
    }


def _release_document(value: Mapping[str, object]) -> dict[str, object]:
    components = value.get("components")
    provenance = value.get("provenance")
    return {
        "release_digest": value.get("release_digest"),
        "lock_digest": value.get("lock_digest"),
        "components": [
            {"name": item.get("name"), "digest": item.get("digest"), "kind": item.get("kind")}
            for item in components if isinstance(item, Mapping)
        ][:128] if isinstance(components, list) else [],
        "dependencies": [item for item in value.get("dependencies", []) if isinstance(item, str)][:256] if isinstance(value.get("dependencies"), list) else [],
        "provenance": [
            {"kind": item.get("kind"), "digest": item.get("digest")}
            for item in provenance if isinstance(item, Mapping)
        ][:128] if isinstance(provenance, list) else [],
    }


def _inventory_document(value: Mapping[str, object]) -> dict[str, object]:
    """Project agent inventory into the bounded, UI-safe wire shape."""

    nodes: list[dict[str, object]] = []
    raw_nodes = value.get("nodes")
    if isinstance(raw_nodes, list):
        for raw_node in raw_nodes[:512]:
            if not isinstance(raw_node, Mapping):
                continue
            storage = raw_node.get("storage")
            resources = raw_node.get("resources")
            packages: list[dict[str, object]] = []
            raw_packages = raw_node.get("packages")
            if isinstance(raw_packages, list):
                for raw_package in raw_packages[:2048]:
                    if not isinstance(raw_package, Mapping):
                        continue
                    envelope = raw_package.get("resources")
                    packages.append(
                        {
                            "deployment_id": raw_package.get("deployment_id"),
                            "family_id": raw_package.get("family_id"),
                            "release_digest": raw_package.get("release_digest"),
                            "content_group": raw_package.get("content_group"),
                            "state": raw_package.get("state"),
                            "bytes_total": raw_package.get("bytes_total"),
                            "bytes_complete": raw_package.get("bytes_complete"),
                            "bytes_remaining": raw_package.get("bytes_remaining"),
                            "installed_bytes": raw_package.get("installed_bytes"),
                            "reclaimable_bytes": raw_package.get("reclaimable_bytes"),
                            "reserved_bytes": raw_package.get("reserved_bytes"),
                            "active": raw_package.get("active"),
                            "retained": raw_package.get("retained"),
                            "leased": raw_package.get("leased"),
                            "operation_id": raw_package.get("operation_id"),
                            "last_operation_state": raw_package.get("last_operation_state"),
                            "last_operation_error": raw_package.get("last_operation_error"),
                            "resources": dict(envelope) if isinstance(envelope, Mapping) else {},
                        }
                    )
            nodes.append(
                {
                    "node_id": raw_node.get("node_id"),
                    "online": raw_node.get("online"),
                    "observed_at": raw_node.get("observed_at"),
                    "storage": dict(storage) if isinstance(storage, Mapping) else {},
                    "resources": dict(resources) if isinstance(resources, Mapping) else {},
                    "current_generation": raw_node.get("current_generation"),
                    "packages": packages,
                }
            )
    return {
        "nodes": nodes,
        "next_cursor": value.get("next_cursor"),
        "total": value.get("total", len(nodes)),
    }


def _removal_preview_document(value: Mapping[str, object]) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    raw_nodes = value.get("nodes")
    if isinstance(raw_nodes, list):
        for raw_node in raw_nodes[:512]:
            if not isinstance(raw_node, Mapping):
                continue
            dependencies = raw_node.get("dependencies")
            nodes.append(
                {
                    "node_id": raw_node.get("node_id"),
                    "state": raw_node.get("state"),
                    "active": raw_node.get("active"),
                    "retained": raw_node.get("retained"),
                    "leased": raw_node.get("leased"),
                    "reclaimable_bytes": raw_node.get("reclaimable_bytes", 0),
                    "dependencies": [item for item in dependencies if isinstance(item, str)][:256]
                    if isinstance(dependencies, list)
                    else [],
                    "blocked_reason": raw_node.get("blocked_reason"),
                }
            )
    blocked_nodes = value.get("blocked_nodes")
    return {
        "digest": value.get("digest"),
        "state": value.get("state"),
        "deployment_id": value.get("deployment_id"),
        "release_digest": value.get("release_digest"),
        "nodes": nodes,
        "reclaimable_bytes": value.get("reclaimable_bytes", 0),
        "blocked_nodes": [item for item in blocked_nodes if isinstance(item, str)][:512]
        if isinstance(blocked_nodes, list)
        else [],
    }


@dataclass(frozen=True)
class PackageApiServices:
    """Narrow route-facing boundary over W11--W14 durable package services."""

    families: Callable[[str | None, int], Mapping[str, object]]
    candidates: Callable[[str | None, str | None, int], Mapping[str, object]]
    candidate: Callable[[str], Mapping[str, object]]
    resolution: Callable[[str], Mapping[str, object]]
    compatibility: Callable[[str], Mapping[str, object]]
    validation_preview: Callable[[str], Mapping[str, object]]
    validate: Callable[[str, str, str, str], Mapping[str, object]]
    validation_status: Callable[[str], Mapping[str, object]]
    promotion_preview: Callable[[str], Mapping[str, object]]
    promote: Callable[[str, str, str, str], Mapping[str, object]]
    deployments: Callable[[str | None, int], Mapping[str, object]]
    deployment: Callable[[str], Mapping[str, object]]
    rollout_preview: Callable[[str], Mapping[str, object]]
    rollout: Callable[[str, str, str, str], Mapping[str, object]]
    rollout_status: Callable[[str, str, str | None, int], Mapping[str, object]]
    rollback_preview: Callable[[str, str], Mapping[str, object]]
    rollback: Callable[[str, str, str, str, str], Mapping[str, object]]
    repair_preview: Callable[[str], Mapping[str, object]]
    repair: Callable[[str, str, str, str], Mapping[str, object]]
    gc_preview: Callable[[], Mapping[str, object]]
    gc: Callable[[str, str, str], Mapping[str, object]]
    inventory: Callable[[str | None, str | None, str | None, int], Mapping[str, object]]
    removal_preview: Callable[[str, str, tuple[str, ...]], Mapping[str, object]]
    remove: Callable[[str, str, str], Mapping[str, object]]
    idempotency: Callable[
        [str, str, tuple[object, ...], Callable[[], Mapping[str, object]]],
        tuple[Mapping[str, object], bool],
    ]

    @classmethod
    def from_object(cls, value: object) -> PackageApiServices:
        names = (
            "families", "candidates", "candidate", "resolution", "compatibility",
            "validation_preview", "validate", "validation_status", "promotion_preview", "promote",
            "deployments", "deployment", "rollout_preview", "rollout", "rollout_status",
            "rollback_preview", "rollback", "repair_preview", "repair", "gc_preview", "gc",
            "inventory", "removal_preview", "remove", "idempotency",
        )
        methods = {name: getattr(value, name, None) for name in names}
        if any(not callable(method) for method in methods.values()):
            raise TypeError("package API services are incomplete")
        return cls(**methods)  # type: ignore[arg-type]


def install_package_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    services: PackageApiServices | None,
) -> None:
    """Mount package administration beneath the existing authenticated API."""

    # Register at mount time as well as naming each route explicitly: callers
    # that construct an app and then derive its admin schema do not depend on
    # the client-generator process having mutated the operation registry.
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(PACKAGE_OPERATION_IDS)

    def package_services() -> PackageApiServices:
        if services is None:
            raise HTTPException(status_code=503, detail="package administration unavailable")
        return services

    def operator(authenticated: Actor) -> None:
        if authenticated.role not in {"operator", "administrator"}:
            raise HTTPException(status_code=403, detail="insufficient role")

    def administrator(authenticated: Actor) -> None:
        if authenticated.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def read(call: Callable[[], Mapping[str, object]], unavailable: str) -> Mapping[str, object]:
        try:
            result = dict(call())
            if isinstance(result.get("failure"), str):
                result["failure"] = "package operation failed"
            return result
        except KeyError:
            raise HTTPException(status_code=404, detail="package resource not found") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="package request is invalid") from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(status_code=503, detail=unavailable) from None

    def mutate(
        action: str,
        request: Request,
        actor: Actor,
        call: Callable[[], Mapping[str, object]],
        *,
        targets: tuple[str, ...],
        digest: str,
    ) -> Mapping[str, object]:
        fingerprint = (actor.subject, action, targets, digest)
        try:
            stored, replayed = package_services().idempotency(
                    actor.subject, request.state.request_id, fingerprint, call
                )
            result = dict(stored)
        except KeyError:
            raise HTTPException(status_code=409, detail="package preview or plan digest is stale") from None
        except ValueError:
            raise HTTPException(status_code=409, detail="package mutation rejected") from None
        except (OSError, RuntimeError, TypeError):
            raise HTTPException(status_code=503, detail="package dispatch unavailable") from None
        if isinstance(result.get("failure"), str):
            result["failure"] = "package operation failed"
        if not replayed:
            audits.append(
                AuditRecord(request.state.request_id, actor.subject, action, None, targets)
            )
        return result

    authenticated = actor_dependency

    @app.get("/api/v1/packages/families", response_model=PackageFamiliesResponse, responses=bounded_error_responses(401, 422, 503), operation_id="listPackageFamilies")
    def list_families(cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=20, ge=1, le=100), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().families(cursor, limit), "package family projection unavailable")

    @app.get("/api/v1/packages/candidates", response_model=PackageCandidatesResponse, responses=bounded_error_responses(401, 422, 503), operation_id="listPackageCandidates")
    def list_candidates(family_id: str | None = Query(default=None, pattern=_IDENTIFIER), cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=20, ge=1, le=100), _actor: Actor = authenticated) -> Mapping[str, object]:
        document = read(lambda: package_services().candidates(family_id, cursor, limit), "package candidate projection unavailable")
        return {**document, "candidates": [_candidate_document(item) for item in document.get("candidates", []) if isinstance(item, Mapping)]}

    @app.get("/api/v1/packages/candidates/{candidate_id}", response_model=PackageCandidateResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageCandidate")
    def get_candidate(candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), _actor: Actor = authenticated) -> Mapping[str, object]:
        return _candidate_document(read(lambda: package_services().candidate(candidate_id), "package candidate projection unavailable"))

    @app.get("/api/v1/packages/candidates/{candidate_id}/resolution", response_model=PackageResolutionResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageResolution")
    def get_resolution(candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().resolution(candidate_id), "package resolution unavailable")

    @app.get("/api/v1/packages/candidates/{candidate_id}/compatibility", response_model=PackageCompatibilityResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageCompatibility")
    def get_compatibility(candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().compatibility(candidate_id), "package compatibility unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/validation-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackageValidation")
    def preview_validation(candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return read(lambda: package_services().validation_preview(candidate_id), "package validation unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/validate", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="validatePackageCandidate")
    def validate_candidate(body: PackagePlanRequest, request: Request, candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return mutate("package.validate", request, authenticated, lambda: package_services().validate(candidate_id, body.plan_digest, authenticated.subject, request.state.request_id), targets=(candidate_id,), digest=body.plan_digest)

    @app.get("/api/v1/packages/validations/{validation_id}", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageValidation")
    def get_validation(validation_id: str = ApiPath(pattern=_UUID), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().validation_status(validation_id), "package validation status unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/promotion-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackagePromotion")
    def preview_promotion(candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return read(lambda: package_services().promotion_preview(candidate_id), "package promotion unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/promote", response_model=PackagePromotionResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="promotePackage")
    def promote_package(body: PackagePromotionRequest, request: Request, candidate_id: str = ApiPath(pattern=_CANDIDATE_ID), authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return mutate("package.promote", request, authenticated, lambda: package_services().promote(candidate_id, body.preview_digest, authenticated.subject, request.state.request_id), targets=(candidate_id,), digest=body.preview_digest)

    @app.get("/api/v1/deployments", response_model=DeploymentsResponse, responses=bounded_error_responses(401, 422, 503), operation_id="listPackageDeployments")
    def list_deployments(cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=20, ge=1, le=100), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().deployments(cursor, limit), "package deployment projection unavailable")

    @app.get("/api/v1/deployments/{deployment_id}", response_model=DeploymentResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageDeployment")
    def get_deployment(deployment_id: str = ApiPath(pattern=_IDENTIFIER), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().deployment(deployment_id), "package deployment projection unavailable")

    @app.post("/api/v1/deployments/{deployment_id}/rollout-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackageRollout")
    def preview_rollout(deployment_id: str = ApiPath(pattern=_IDENTIFIER), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return read(lambda: package_services().rollout_preview(deployment_id), "package rollout unavailable")

    @app.post("/api/v1/deployments/{deployment_id}/rollouts", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="rolloutPackageDeployment")
    def rollout_deployment(body: PackagePlanRequest, request: Request, deployment_id: str = ApiPath(pattern=_IDENTIFIER), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return mutate("package.rollout", request, authenticated, lambda: package_services().rollout(deployment_id, body.plan_digest, authenticated.subject, request.state.request_id), targets=(deployment_id,), digest=body.plan_digest)

    @app.get("/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageRollout")
    def get_rollout(deployment_id: str = ApiPath(pattern=_IDENTIFIER), rollout_id: str = ApiPath(pattern=_UUID), cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=20, ge=1, le=100), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().rollout_status(deployment_id, rollout_id, cursor, limit), "package rollout status unavailable")

    @app.post("/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}/rollback-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackageRollback")
    def preview_rollback(deployment_id: str = ApiPath(pattern=_IDENTIFIER), rollout_id: str = ApiPath(pattern=_UUID), authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return read(lambda: package_services().rollback_preview(deployment_id, rollout_id), "package rollback unavailable")

    @app.post("/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}/rollback", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="rollbackPackageDeployment")
    def rollback_deployment(body: PackagePlanRequest, request: Request, deployment_id: str = ApiPath(pattern=_IDENTIFIER), rollout_id: str = ApiPath(pattern=_UUID), authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return mutate("package.rollback", request, authenticated, lambda: package_services().rollback(deployment_id, rollout_id, body.plan_digest, authenticated.subject, request.state.request_id), targets=(deployment_id, rollout_id), digest=body.plan_digest)

    @app.post("/api/v1/deployments/{deployment_id}/repair-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackageRepair")
    def preview_repair(deployment_id: str = ApiPath(pattern=_IDENTIFIER), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return read(lambda: package_services().repair_preview(deployment_id), "package repair unavailable")

    @app.post("/api/v1/deployments/{deployment_id}/repair", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="repairPackageDeployment")
    def repair_deployment(body: PackagePlanRequest, request: Request, deployment_id: str = ApiPath(pattern=_IDENTIFIER), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return mutate("package.repair", request, authenticated, lambda: package_services().repair(deployment_id, body.plan_digest, authenticated.subject, request.state.request_id), targets=(deployment_id,), digest=body.plan_digest)

    @app.get("/api/v1/packages/inventory", response_model=PackageInventoryResponse, responses=bounded_error_responses(401, 422, 503), operation_id="listPackageInventory")
    def list_inventory(
        node_id: str | None = Query(default=None, max_length=128),
        deployment_id: str | None = Query(default=None, pattern=_IDENTIFIER),
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=20, ge=1, le=100),
        _actor: Actor = authenticated,
    ) -> Mapping[str, object]:
        return _inventory_document(
            read(
                lambda: package_services().inventory(node_id, deployment_id, cursor, limit),
                "package inventory projection unavailable",
            )
        )

    @app.post("/api/v1/packages/inventory/remove-preview", response_model=PackageRemovalPreviewResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackageRemoval")
    def preview_removal(
        body: PackageRemovalRequest,
        authenticated: Actor = authenticated,
    ) -> Mapping[str, object]:
        operator(authenticated)
        return _removal_preview_document(
            read(
                lambda: package_services().removal_preview(
                    body.deployment_id, body.release_digest, tuple(body.node_ids)
                ),
                "package removal preview unavailable",
            )
        )

    @app.post("/api/v1/packages/inventory/remove", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="removePackageInventory")
    def remove_inventory(
        body: PackagePlanRequest,
        request: Request,
        authenticated: Actor = authenticated,
    ) -> Mapping[str, object]:
        operator(authenticated)
        return mutate(
            "package.remove",
            request,
            authenticated,
            lambda: package_services().remove(
                body.plan_digest, authenticated.subject, request.state.request_id
            ),
            targets=(body.plan_digest,),
            digest=body.plan_digest,
        )

    @app.post("/api/v1/packages/gc-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 409, 422, 503), operation_id="previewPackageGc")
    def preview_gc(authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return read(lambda: package_services().gc_preview(), "package garbage collection unavailable")

    @app.post("/api/v1/packages/gc", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="applyPackageGc")
    def apply_gc(body: PackagePlanRequest, request: Request, authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return mutate("package.gc", request, authenticated, lambda: package_services().gc(body.plan_digest, authenticated.subject, request.state.request_id), targets=("gc",), digest=body.plan_digest)


__all__ = [
    "PACKAGE_OPERATION_IDS",
    "PackageApiServices",
    "PackageCompatibilityResponse",
    "PackageInventoryResponse",
    "PackagePlanResponse",
    "PackageProgressResponse",
    "PackagePromotionResponse",
    "PackageRemovalPreviewResponse",
    "install_package_routes",
]
