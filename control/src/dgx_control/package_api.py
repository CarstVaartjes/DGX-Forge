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
_RAW_DIGEST = r"^[0-9a-f]{64}$"
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
    id: str = Field(pattern=_RAW_DIGEST)
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
    candidate_id: str = Field(pattern=_RAW_DIGEST)
    release_digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)


class PackageCompatibilityResponse(StrictModel):
    candidate_id: str = Field(pattern=_RAW_DIGEST)
    release_digest: str = Field(pattern=_DIGEST)
    digest: str = Field(pattern=_DIGEST)
    compatible_node_ids: list[str] = Field(max_length=512)


class PackagePlanResponse(StrictModel):
    digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)
    candidate_id: str | None = Field(default=None, pattern=_RAW_DIGEST)
    deployment_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    release_digest: str | None = Field(default=None, pattern=_DIGEST)
    reclaim_bytes: int | None = Field(default=None, ge=0)
    batches: list[list[str]] = Field(default_factory=list, max_length=128)
    canary_node: str | None = Field(default=None, max_length=128)
    offline_pending: list[str] = Field(default_factory=list, max_length=512)
    storage_bytes: int | None = Field(default=None, ge=0)
    download_bytes: int | None = Field(default=None, ge=0)


class PackagePlanRequest(StrictModel):
    plan_digest: str = Field(pattern=_DIGEST)


class PackagePromotionRequest(StrictModel):
    preview_digest: str = Field(pattern=_DIGEST)


class PackagePromotionResponse(StrictModel):
    candidate_id: str = Field(pattern=_RAW_DIGEST)
    release_digest: str = Field(pattern=_DIGEST)
    digest: str = Field(pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)


class DeploymentResponse(StrictModel):
    id: str = Field(pattern=_IDENTIFIER)
    family_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    release_digest: str = Field(pattern=_DIGEST)
    previous_release_digest: str | None = Field(default=None, pattern=_DIGEST)
    state: str = Field(min_length=1, max_length=64)


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
            "rollback_preview", "rollback", "repair_preview", "repair", "gc_preview", "gc", "idempotency",
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
    def get_candidate(candidate_id: str = ApiPath(pattern=_RAW_DIGEST), _actor: Actor = authenticated) -> Mapping[str, object]:
        return _candidate_document(read(lambda: package_services().candidate(candidate_id), "package candidate projection unavailable"))

    @app.get("/api/v1/packages/candidates/{candidate_id}/resolution", response_model=PackageResolutionResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageResolution")
    def get_resolution(candidate_id: str = ApiPath(pattern=_RAW_DIGEST), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().resolution(candidate_id), "package resolution unavailable")

    @app.get("/api/v1/packages/candidates/{candidate_id}/compatibility", response_model=PackageCompatibilityResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageCompatibility")
    def get_compatibility(candidate_id: str = ApiPath(pattern=_RAW_DIGEST), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().compatibility(candidate_id), "package compatibility unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/validation-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackageValidation")
    def preview_validation(candidate_id: str = ApiPath(pattern=_RAW_DIGEST), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return read(lambda: package_services().validation_preview(candidate_id), "package validation unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/validate", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="validatePackageCandidate")
    def validate_candidate(body: PackagePlanRequest, request: Request, candidate_id: str = ApiPath(pattern=_RAW_DIGEST), authenticated: Actor = authenticated) -> Mapping[str, object]:
        operator(authenticated)
        return mutate("package.validate", request, authenticated, lambda: package_services().validate(candidate_id, body.plan_digest, authenticated.subject, request.state.request_id), targets=(candidate_id,), digest=body.plan_digest)

    @app.get("/api/v1/packages/validations/{validation_id}", response_model=PackageProgressResponse, responses=bounded_error_responses(401, 404, 422, 503), operation_id="getPackageValidation")
    def get_validation(validation_id: str = ApiPath(pattern=_UUID), _actor: Actor = authenticated) -> Mapping[str, object]:
        return read(lambda: package_services().validation_status(validation_id), "package validation status unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/promotion-preview", response_model=PackagePlanResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), operation_id="previewPackagePromotion")
    def preview_promotion(candidate_id: str = ApiPath(pattern=_RAW_DIGEST), authenticated: Actor = authenticated) -> Mapping[str, object]:
        administrator(authenticated)
        return read(lambda: package_services().promotion_preview(candidate_id), "package promotion unavailable")

    @app.post("/api/v1/packages/candidates/{candidate_id}/promote", response_model=PackagePromotionResponse, responses=bounded_error_responses(401, 403, 404, 409, 422, 503), status_code=status.HTTP_202_ACCEPTED, operation_id="promotePackage")
    def promote_package(body: PackagePromotionRequest, request: Request, candidate_id: str = ApiPath(pattern=_RAW_DIGEST), authenticated: Actor = authenticated) -> Mapping[str, object]:
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
    "PackagePlanResponse",
    "PackageProgressResponse",
    "PackagePromotionResponse",
    "install_package_routes",
]
