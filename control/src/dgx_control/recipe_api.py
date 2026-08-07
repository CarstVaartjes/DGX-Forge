"""Typed HTTP workflow for recipe admission and lifecycle operations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .audit import AuditRecord
from .auth import Actor
from .recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeOperationView,
)
from .topology import Placement

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_NODE = r"^spk_[0-9a-f]{32}$"
_DIGEST = r"^[0-9a-f]{64}$"

RECIPE_OPERATION_IDS = {
    ("post", "/api/v1/recipes/install-plans/preview"): "previewRecipeInstall",
    ("post", "/api/v1/recipes/installations"): "installRecipe",
    ("post", "/api/v1/recipes/run-plans/preview"): "previewRecipeRun",
    ("post", "/api/v1/recipes/runs"): "startRecipeRun",
    ("get", "/api/v1/recipes/operations/{operation_id}"): "getRecipeOperation",
    ("post", "/api/v1/recipes/operations/{operation_id}/retry"): "retryRecipeOperation",
    ("post", "/api/v1/recipes/runs/{run_id}/stop"): "stopRecipeRun",
    ("post", "/api/v1/recipes/installations/{installation_id}/uninstall"): "uninstallRecipe",
}


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanReason(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=512)


class InstallNodePlanResponse(StrictModel):
    node_id: str
    allowed: bool
    inventory_observed_at: datetime | None
    free_bytes: int | None
    active_reserved_bytes: int
    reused_bytes: int
    required_download_bytes: int
    required_bytes: int
    disk_floor_bytes: int
    free_after_bytes: int | None
    blockers: list[PlanReason]
    warnings: list[PlanReason]


class InstallPlanResponse(StrictModel):
    recipe_revision_id: str
    recipe_content_sha256: str
    allowed: bool
    nodes: list[InstallNodePlanResponse]
    plan_digest: str


class RunNodePlanResponse(StrictModel):
    node_id: str
    rank: int
    role: str
    port: int
    allowed: bool
    inventory_observed_at: datetime | None
    required_memory_bytes: int
    host_free_bytes: int | None
    gpu_free_bytes: int | None
    active_host_reserved_bytes: int
    active_gpu_reserved_bytes: int
    free_after_bytes: int | None
    memory_floor_bytes: int
    blockers: list[PlanReason]
    warnings: list[PlanReason]


class RunPlanResponse(StrictModel):
    installation_id: str
    recipe_revision_id: str
    allowed: bool
    nodes: list[RunNodePlanResponse]
    plan_digest: str


class OperationResponse(StrictModel):
    id: str
    kind: str
    owner_id: str
    state: str
    plan_digest: str
    nodes: list[str]
    result: dict[str, object] | None


class InstallPreviewRequest(StrictModel):
    recipe_revision_id: str = Field(pattern=_UUID)
    node_ids: list[str] = Field(min_length=1, max_length=64)


class InstallRequest(InstallPreviewRequest):
    plan_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class PlacementRequest(StrictModel):
    node_id: str = Field(pattern=_NODE)
    rank: int = Field(ge=0, le=1023, strict=True)
    role: Literal["entrypoint", "worker"]


class RunPreviewRequest(StrictModel):
    installation_id: str = Field(pattern=_UUID)
    placements: list[PlacementRequest] = Field(min_length=1, max_length=64)


class RunRequest(RunPreviewRequest):
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    plan_digest: str = Field(pattern=_DIGEST)
    request_key: str = Field(pattern=_UUID)


class RequestKey(StrictModel):
    request_key: str = Field(pattern=_UUID)


def install_recipe_operation_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    service: RecipeOperationService | None,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(RECIPE_OPERATION_IDS)
    authenticated = actor_dependency

    def recipes() -> RecipeOperationService:
        if service is None:
            raise HTTPException(status_code=503, detail="recipe operations unavailable")
        return service

    def administrator(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def placements(values: list[PlacementRequest]) -> tuple[Placement, ...]:
        return tuple(Placement(item.node_id, item.rank, item.role) for item in values)

    def operation(value: RecipeOperationView) -> dict[str, object]:
        return {
            "id": value.id,
            "kind": value.kind,
            "owner_id": value.owner_id,
            "state": value.state,
            "plan_digest": value.plan_digest,
            "nodes": list(value.nodes),
            "result": value.result,
        }

    def conflict(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": "recipe.operation_conflict",
                "detail": str(error)[:256],
                "request_id": request.state.request_id,
            },
        )

    @app.post(
        "/api/v1/recipes/install-plans/preview",
        response_model=InstallPlanResponse,
        operation_id="previewRecipeInstall",
    )
    def preview_install(body: InstallPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        return asdict(recipes().preview_install(body.recipe_revision_id, tuple(body.node_ids)))

    @app.post(
        "/api/v1/recipes/installations",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="installRecipe",
    )
    def install(body: InstallRequest, request: Request, actor: Actor = authenticated):
        administrator(actor)
        try:
            plan = recipes().preview_install(body.recipe_revision_id, tuple(body.node_ids))
            value = recipes().install(
                plan,
                plan_digest=body.plan_digest,
                actor=actor.subject,
                request_id=body.request_key,
            )
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "recipe.install", None, (value.owner_id, value.plan_digest, *value.nodes)))
        return operation(value)

    @app.post(
        "/api/v1/recipes/run-plans/preview",
        response_model=RunPlanResponse,
        operation_id="previewRecipeRun",
    )
    def preview_run(body: RunPreviewRequest, actor: Actor = authenticated):
        administrator(actor)
        return asdict(recipes().preview_run(body.installation_id, placements(body.placements)))

    @app.post(
        "/api/v1/recipes/runs",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="startRecipeRun",
    )
    def start(body: RunRequest, request: Request, actor: Actor = authenticated):
        administrator(actor)
        try:
            plan = recipes().preview_run(body.installation_id, placements(body.placements))
            value = recipes().start(plan, plan_digest=body.plan_digest, alias=body.alias, actor=actor.subject, request_id=body.request_key)
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "recipe.start", None, (value.owner_id, value.plan_digest, *value.nodes)))
        return operation(value)

    @app.get(
        "/api/v1/recipes/operations/{operation_id}",
        response_model=OperationResponse,
        operation_id="getRecipeOperation",
    )
    def get_operation(operation_id: str = Path(pattern=_UUID), _actor: Actor = authenticated):
        try:
            return operation(recipes().get(operation_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe operation not found") from None

    @app.post(
        "/api/v1/recipes/operations/{operation_id}/retry",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="retryRecipeOperation",
    )
    def retry(body: RequestKey, request: Request, operation_id: str = Path(pattern=_UUID), actor: Actor = authenticated):
        administrator(actor)
        try:
            value = recipes().retry(operation_id, actor=actor.subject, request_id=body.request_key)
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "recipe.retry", None, (operation_id, value.id)))
        return operation(value)

    @app.post(
        "/api/v1/recipes/runs/{run_id}/stop",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="stopRecipeRun",
    )
    def stop(body: RequestKey, request: Request, run_id: str = Path(pattern=_UUID), actor: Actor = authenticated):
        administrator(actor)
        try:
            value = recipes().stop(run_id, actor=actor.subject, request_id=body.request_key)
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "recipe.stop", None, (run_id, value.plan_digest, *value.nodes)))
        return operation(value)

    @app.post(
        "/api/v1/recipes/installations/{installation_id}/uninstall",
        response_model=OperationResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="uninstallRecipe",
    )
    def uninstall(body: RequestKey, request: Request, installation_id: str = Path(pattern=_UUID), actor: Actor = authenticated):
        administrator(actor)
        try:
            value = recipes().uninstall(installation_id, actor=actor.subject, request_id=body.request_key)
        except RecipeOperationConflict as error:
            return conflict(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "recipe.uninstall", None, (installation_id, value.plan_digest, *value.nodes)))
        return operation(value)


__all__ = ["RECIPE_OPERATION_IDS", "install_recipe_operation_routes"]
