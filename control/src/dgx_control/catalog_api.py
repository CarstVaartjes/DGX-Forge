"""Strict authenticated HTTP surface for the local database recipe catalog."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .audit import AuditRecord
from .auth import Actor
from .catalog_service import (
    CatalogConflict,
    CatalogError,
    CatalogService,
    RecipeDraftInput,
    RecipeRevisionView,
    RecipeSummary,
)

_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SLUG = r"^[a-z0-9][a-z0-9-]{1,62}$"
_MAX_DOCUMENT_BYTES = 256 * 1024

CATALOG_OPERATION_IDS = {
    ("get", "/api/v1/catalog/recipes"): "listLocalRecipes",
    ("post", "/api/v1/catalog/recipes"): "createLocalRecipe",
    ("get", "/api/v1/catalog/recipes/{recipe_id}"): "getLocalRecipe",
    ("put", "/api/v1/catalog/recipes/{recipe_id}/draft"): "updateLocalRecipeDraft",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/resolve"): "resolveLocalRecipe",
    ("post", "/api/v1/catalog/recipes/{recipe_id}/fork"): "forkLocalRecipe",
}


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogProblem(StrictModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)
    request_id: str = Field(pattern=_UUID)


class CreateRecipeRequest(StrictModel):
    slug: str = Field(pattern=_SLUG)
    document: dict[str, object]


class UpdateRecipeDraftRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)
    document: dict[str, object]


class ResolveRecipeRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)


class ForkRecipeRequest(StrictModel):
    revision: int = Field(ge=1, strict=True)
    slug: str = Field(pattern=_SLUG)


class RecipeSummaryResponse(StrictModel):
    recipe_id: str = Field(pattern=_UUID)
    slug: str = Field(pattern=_SLUG)
    title: str = Field(min_length=1, max_length=120)
    origin: Literal["local", "sparkrun", "global"]
    revision_number: int = Field(ge=1)
    lifecycle: Literal["draft", "blocked", "resolved", "deprecated"]
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RecipeListResponse(StrictModel):
    recipes: list[RecipeSummaryResponse] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=64)


class RecipeRevisionResponse(RecipeSummaryResponse):
    id: str = Field(pattern=_UUID)
    description: str = Field(min_length=1, max_length=4000)
    schema_version: Literal[1]
    document: dict[str, object]
    created_by: str = Field(min_length=1, max_length=200)
    created_at: str


def _problem(request: Request, error: CatalogError) -> JSONResponse:
    status_code = 409 if isinstance(error, CatalogConflict) else 422
    return JSONResponse(
        status_code=status_code,
        content={
            "code": error.code[:128],
            "detail": error.detail[:256],
            "request_id": request.state.request_id,
        },
    )


def _summary(value: RecipeSummary) -> dict[str, object]:
    return {
        "recipe_id": value.recipe_id,
        "slug": value.slug,
        "title": value.title,
        "origin": value.source_kind,
        "revision_number": value.revision_number,
        "lifecycle": value.lifecycle,
        "content_sha256": value.content_sha256,
    }


def _revision(value: RecipeRevisionView) -> dict[str, object]:
    return {
        "recipe_id": value.recipe_id,
        "slug": value.slug,
        "title": value.title,
        "origin": value.source_kind,
        "revision_number": value.revision_number,
        "lifecycle": value.lifecycle,
        "content_sha256": value.content_sha256,
        "id": value.id,
        "description": value.description,
        "schema_version": value.schema_version,
        "document": value.document,
        "created_by": value.created_by,
        "created_at": value.created_at.isoformat(),
    }


def _bounded(document: Mapping[str, object]) -> None:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise CatalogError("catalog.document_too_large", "recipe document exceeds 256 KiB")


def install_catalog_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    service: CatalogService | None,
) -> None:
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(CATALOG_OPERATION_IDS)
    authenticated = actor_dependency

    def catalog() -> CatalogService:
        if service is None:
            raise HTTPException(status_code=503, detail="catalog unavailable")
        return service

    def administrator(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def read(call, request: Request):
        try:
            return call()
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)

    @app.get(
        "/api/v1/catalog/recipes",
        response_model=RecipeListResponse,
        responses={401: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        operation_id="listLocalRecipes",
    )
    def list_recipes(
        request: Request,
        cursor: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
        _actor: Actor = authenticated,
    ):
        result = read(lambda: catalog().list_recipes(limit=limit, cursor=cursor), request)
        if isinstance(result, JSONResponse):
            return result
        recipes, next_cursor = result
        return {"recipes": [_summary(item) for item in recipes], "next_cursor": next_cursor}

    @app.post(
        "/api/v1/catalog/recipes",
        response_model=RecipeRevisionResponse,
        responses={401: {"model": CatalogProblem}, 403: {"model": CatalogProblem}, 409: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        status_code=status.HTTP_201_CREATED,
        operation_id="createLocalRecipe",
    )
    def create_recipe(body: CreateRecipeRequest, request: Request, actor: Actor = authenticated):
        administrator(actor)
        try:
            _bounded(body.document)
            result = catalog().create_recipe(
                actor.subject, RecipeDraftInput(slug=body.slug, document=body.document)
            )
        except CatalogError as error:
            return _problem(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "catalog.recipe.create", None, (result.recipe_id,)))
        return _revision(result)

    @app.get(
        "/api/v1/catalog/recipes/{recipe_id}",
        response_model=RecipeRevisionResponse,
        responses={401: {"model": CatalogProblem}, 404: {"model": CatalogProblem}},
        operation_id="getLocalRecipe",
    )
    def get_recipe(request: Request, recipe_id: str = Path(pattern=_UUID), _actor: Actor = authenticated):
        result = read(lambda: catalog().get_recipe(recipe_id), request)
        return result if isinstance(result, JSONResponse) else _revision(result)

    @app.put(
        "/api/v1/catalog/recipes/{recipe_id}/draft",
        response_model=RecipeRevisionResponse,
        responses={401: {"model": CatalogProblem}, 403: {"model": CatalogProblem}, 404: {"model": CatalogProblem}, 409: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        operation_id="updateLocalRecipeDraft",
    )
    def update_draft(body: UpdateRecipeDraftRequest, request: Request, recipe_id: str = Path(pattern=_UUID), actor: Actor = authenticated):
        administrator(actor)
        try:
            _bounded(body.document)
            result = catalog().update_draft(recipe_id, body.expected_revision, body.document, actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "catalog.recipe.update", None, (recipe_id, str(result.revision_number))))
        return _revision(result)

    @app.post(
        "/api/v1/catalog/recipes/{recipe_id}/resolve",
        response_model=RecipeRevisionResponse,
        responses={401: {"model": CatalogProblem}, 403: {"model": CatalogProblem}, 404: {"model": CatalogProblem}, 409: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        operation_id="resolveLocalRecipe",
    )
    def resolve_recipe(body: ResolveRecipeRequest, request: Request, recipe_id: str = Path(pattern=_UUID), actor: Actor = authenticated):
        administrator(actor)
        try:
            result = catalog().resolve(recipe_id, body.expected_revision, actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "catalog.recipe.resolve", None, (recipe_id, result.content_sha256 or "")))
        return _revision(result)

    @app.post(
        "/api/v1/catalog/recipes/{recipe_id}/fork",
        response_model=RecipeRevisionResponse,
        responses={401: {"model": CatalogProblem}, 403: {"model": CatalogProblem}, 404: {"model": CatalogProblem}, 409: {"model": CatalogProblem}, 422: {"model": CatalogProblem}},
        status_code=status.HTTP_201_CREATED,
        operation_id="forkLocalRecipe",
    )
    def fork_recipe(body: ForkRecipeRequest, request: Request, recipe_id: str = Path(pattern=_UUID), actor: Actor = authenticated):
        administrator(actor)
        try:
            result = catalog().fork(recipe_id, body.revision, body.slug, actor.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except CatalogError as error:
            return _problem(request, error)
        audits.append(AuditRecord(request.state.request_id, actor.subject, "catalog.recipe.fork", None, (recipe_id, result.recipe_id)))
        return _revision(result)


__all__ = ["CATALOG_OPERATION_IDS", "CatalogProblem", "install_catalog_routes"]
