"""Database-authoritative local recipe authoring and immutable resolution."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .catalog_repository import CatalogRepository, sensitive_document_path
from .models import LocalRecipe, LocalRecipeRevision
from .recipe_contract import (
    RecipeContractError,
    recipe_content_sha256,
    validate_recipe,
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MUTABLE_REVISIONS = frozenset(
    {"main", "master", "latest", "head", "main-latest", "master-latest"}
)


class CatalogError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class CatalogConflict(CatalogError):
    pass


class CatalogValidationError(CatalogError):
    pass


@dataclass(frozen=True, slots=True)
class RecipeDraftInput:
    slug: str
    document: Mapping[str, object]
    source_kind: str = "local"


@dataclass(frozen=True, slots=True)
class RecipeRevisionView:
    id: str
    recipe_id: str
    slug: str
    title: str
    description: str
    source_kind: str
    revision_number: int
    lifecycle: str
    schema_version: int
    document: dict[str, object]
    content_sha256: str | None
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecipeSummary:
    recipe_id: str
    slug: str
    title: str
    source_kind: str
    revision_number: int
    lifecycle: str
    content_sha256: str | None
    runtime_family: str
    runtime_image: str
    artifact_count: int
    expected_download_bytes: int
    installed_bytes_per_node: int
    resident_memory_bytes_per_node: int
    activation_memory_bytes_per_node: int
    min_nodes: int
    max_nodes: int


class CatalogService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        repository: CatalogRepository | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._repository = repository or CatalogRepository()

    def list_recipes(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[RecipeSummary], str | None]:
        if not 1 <= limit <= 100:
            raise CatalogValidationError("catalog.limit", "catalog limit is invalid")
        latest_numbers = (
            select(
                LocalRecipeRevision.recipe_id,
                func.max(LocalRecipeRevision.revision_number).label("revision_number"),
            )
            .group_by(LocalRecipeRevision.recipe_id)
            .subquery()
        )
        with self._sessions() as session:
            statement = (
                select(LocalRecipe, LocalRecipeRevision)
                .join(latest_numbers, latest_numbers.c.recipe_id == LocalRecipe.id)
                .join(
                    LocalRecipeRevision,
                    and_(
                        LocalRecipeRevision.recipe_id == LocalRecipe.id,
                        LocalRecipeRevision.revision_number
                        == latest_numbers.c.revision_number,
                    ),
                )
                .order_by(LocalRecipe.updated_at.desc(), LocalRecipe.id.desc())
            )
            if cursor is not None:
                boundary = self._repository.recipe(session, cursor)
                if boundary is None:
                    raise CatalogValidationError(
                        "catalog.cursor", "catalog cursor is invalid"
                    )
                statement = statement.where(
                    or_(
                        LocalRecipe.updated_at < boundary.updated_at,
                        and_(
                            LocalRecipe.updated_at == boundary.updated_at,
                            LocalRecipe.id < boundary.id,
                        ),
                    )
                )
            rows = session.execute(statement.limit(limit + 1)).all()
        page = rows[:limit]
        summaries = [
            _summary(recipe, revision)
            for recipe, revision in page
        ]
        next_cursor = page[-1][0].id if len(rows) > limit else None
        return summaries, next_cursor

    def get_recipe(self, recipe_id: str) -> RecipeRevisionView:
        with self._sessions() as session:
            recipe = self._require_recipe(session, recipe_id)
            revision = self._repository.latest_revision(session, recipe_id)
            if revision is None:
                raise KeyError(recipe_id)
            return _view(recipe, revision)

    def create_recipe(
        self, actor: str, draft: RecipeDraftInput
    ) -> RecipeRevisionView:
        document = self._validated_document(draft.document, slug=draft.slug)
        if draft.source_kind not in {"local", "sparkrun", "global"}:
            raise CatalogValidationError("catalog.source_kind", "unknown source kind")
        metadata = _mapping(document["metadata"])
        now = self._clock()
        recipe = LocalRecipe(
            slug=draft.slug,
            title=str(metadata["title"]),
            description=str(metadata["description"]),
            source_kind=draft.source_kind,
            created_by=_actor(actor),
            created_at=now,
            updated_at=now,
        )
        revision = LocalRecipeRevision(
            recipe_id="",
            revision_number=1,
            lifecycle="draft",
            schema_version=1,
            document=document,
            content_sha256=None,
            created_by=_actor(actor),
            created_at=now,
        )
        # The models intentionally have no relationship cascade; flush the parent first.
        try:
            with self._sessions.begin() as session:
                session.add(recipe)
                session.flush()
                revision.recipe_id = recipe.id
                session.add(revision)
                session.flush()
                view = _view(recipe, revision)
        except IntegrityError as error:
            raise CatalogConflict("catalog.slug_exists", "recipe slug already exists") from error
        return view

    def update_draft(
        self,
        recipe_id: str,
        expected_revision: int,
        document: Mapping[str, object],
        actor: str,
    ) -> RecipeRevisionView:
        with self._sessions.begin() as session:
            recipe = self._require_recipe(session, recipe_id, for_update=True)
            clean = self._validated_document(document, slug=recipe.slug)
            latest = self._repository.latest_revision(session, recipe_id)
            if latest is None or latest.revision_number != expected_revision:
                raise CatalogConflict("catalog.stale_revision", "recipe revision changed")
            if latest.lifecycle not in {"draft", "blocked"}:
                raise CatalogConflict("catalog.resolved", "resolved recipe cannot be edited")
            metadata = _mapping(clean["metadata"])
            now = self._clock()
            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=self._repository.next_revision_number(session, recipe.id),
                lifecycle="draft",
                schema_version=1,
                document=clean,
                content_sha256=None,
                created_by=_actor(actor),
                created_at=now,
            )
            recipe.title = str(metadata["title"])
            recipe.description = str(metadata["description"])
            recipe.updated_at = now
            session.add(revision)
            session.flush()
            return _view(recipe, revision)

    def resolve(
        self, recipe_id: str, expected_revision: int, actor: str
    ) -> RecipeRevisionView:
        with self._sessions.begin() as session:
            recipe = self._require_recipe(session, recipe_id, for_update=True)
            latest = self._repository.latest_revision(session, recipe_id)
            if latest is None:
                raise KeyError(recipe_id)
            if latest.lifecycle == "resolved":
                if expected_revision in {
                    latest.revision_number,
                    latest.revision_number - 1,
                }:
                    return _view(recipe, latest)
                raise CatalogConflict("catalog.stale_revision", "recipe revision changed")
            if latest.revision_number != expected_revision:
                raise CatalogConflict("catalog.stale_revision", "recipe revision changed")
            clean = self._validated_document(latest.document, slug=recipe.slug)
            digest = recipe_content_sha256(clean)
            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=self._repository.next_revision_number(session, recipe.id),
                lifecycle="resolved",
                schema_version=1,
                document=clean,
                content_sha256=digest,
                created_by=_actor(actor),
                created_at=self._clock(),
            )
            session.add(revision)
            session.flush()
            return _view(recipe, revision)

    def fork(
        self,
        recipe_id: str,
        revision_number: int,
        slug: str,
        actor: str,
    ) -> RecipeRevisionView:
        with self._sessions() as session:
            source_recipe = self._require_recipe(session, recipe_id)
            source = self._repository.revision(session, recipe_id, revision_number)
            if source is None:
                raise KeyError((recipe_id, revision_number))
            document = copy.deepcopy(source.document)
        identity = _mapping(document["identity"])
        identity["slug"] = slug
        provenance = _mapping(document["provenance"])
        provenance["source_kind"] = "fork"
        provenance["source_reference"] = f"local:{source_recipe.slug}:{revision_number}"
        attribution = list(provenance.get("attribution", []))
        identity_digest = source.content_sha256 or recipe_content_sha256(source.document)
        attribution.append(f"forked from {source_recipe.slug}@sha256:{identity_digest}")
        provenance["attribution"] = attribution
        return self.create_recipe(
            actor, RecipeDraftInput(slug=slug, document=document, source_kind="local")
        )

    def _require_recipe(
        self, session: Session, recipe_id: str, *, for_update: bool = False
    ) -> LocalRecipe:
        recipe = self._repository.recipe(session, recipe_id, for_update=for_update)
        if recipe is None:
            raise KeyError(recipe_id)
        return recipe

    def _validated_document(
        self, document: Mapping[str, object], *, slug: str
    ) -> dict[str, object]:
        if not _SLUG.fullmatch(slug):
            raise CatalogValidationError("catalog.slug", "recipe slug is invalid")
        sensitive = sensitive_document_path(document)
        if sensitive is not None:
            raise CatalogValidationError(
                "catalog.sensitive_field", f"sensitive field is forbidden at {sensitive}"
            )
        clean: dict[str, object] = copy.deepcopy(dict(document))
        try:
            validate_recipe(clean)
        except RecipeContractError as error:
            raise CatalogValidationError(error.code, f"{error.path}: {error.detail}") from error
        identity = _mapping(clean["identity"])
        if identity.get("slug") != slug:
            raise CatalogValidationError(
                "catalog.slug_mismatch", "recipe identity slug does not match"
            )
        for artifact in clean["artifacts"]:
            revision = str(_mapping(artifact)["revision"]).lower()
            if revision in _MUTABLE_REVISIONS or revision.endswith("-latest"):
                raise CatalogValidationError(
                    "catalog.mutable_artifact", "artifact revision must be immutable"
                )
        return clean


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogValidationError("catalog.document", "recipe object is invalid")
    return value


def _actor(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise CatalogValidationError("catalog.actor", "catalog actor is invalid")
    return normalized


def _view(recipe: LocalRecipe, revision: LocalRecipeRevision) -> RecipeRevisionView:
    return RecipeRevisionView(
        id=revision.id,
        recipe_id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        description=recipe.description,
        source_kind=recipe.source_kind,
        revision_number=revision.revision_number,
        lifecycle=revision.lifecycle,
        schema_version=revision.schema_version,
        document=copy.deepcopy(revision.document),
        content_sha256=revision.content_sha256,
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def _summary(recipe: LocalRecipe, revision: LocalRecipeRevision) -> RecipeSummary:
    runtime = _mapping(revision.document["runtime"])
    resources = _mapping(_mapping(revision.document["resources"])["per_node"])
    topology = _mapping(revision.document["topology"])
    artifacts = revision.document["artifacts"]
    assert isinstance(artifacts, list)
    return RecipeSummary(
        recipe_id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        source_kind=recipe.source_kind,
        revision_number=revision.revision_number,
        lifecycle=revision.lifecycle,
        content_sha256=revision.content_sha256,
        runtime_family=str(runtime["family"]),
        runtime_image=str(runtime["image"]),
        artifact_count=len(artifacts),
        expected_download_bytes=sum(
            int(_mapping(artifact)["expected_bytes"]) for artifact in artifacts
        ),
        installed_bytes_per_node=int(resources["installed_bytes"]),
        resident_memory_bytes_per_node=int(resources["resident_memory_bytes"]),
        activation_memory_bytes_per_node=int(resources["activation_memory_bytes"]),
        min_nodes=int(topology["min_nodes"]),
        max_nodes=int(topology["max_nodes"]),
    )
