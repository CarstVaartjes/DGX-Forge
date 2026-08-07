"""Database-authoritative local recipe authoring and immutable resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .catalog_repository import CatalogRepository, sensitive_document_path
from .global_catalog import GlobalRecipeRevision
from .models import (
    LocalRecipe,
    LocalRecipeRevision,
    RecipeGlobalLink,
    RecipeImport,
    RecipeImportItem,
    RecipeTestReport,
)
from .recipe_contract import (
    RecipeContractError,
    recipe_content_sha256,
    validate_recipe,
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MUTABLE_REVISIONS = frozenset(
    {"main", "master", "latest", "head", "main-latest", "master-latest"}
)
_REQUIRED_TEST_CHECKS = frozenset(
    {"container.started", "endpoint.healthy", "inference.completed"}
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
            if recipe.source_kind == "sparkrun":
                unresolved = session.scalar(
                    select(RecipeImportItem.id)
                    .join(RecipeImport, RecipeImport.id == RecipeImportItem.import_id)
                    .where(
                        RecipeImport.recipe_id == recipe_id,
                        RecipeImportItem.disposition.in_(("resolution_required", "overlay_required", "unsupported_blocking")),
                    )
                    .limit(1)
                )
                if unresolved is not None:
                    raise CatalogConflict(
                        "catalog.import_unresolved",
                        "import report must be resolved before this recipe can run",
                    )
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

    def import_global(
        self, actor: str, remote: GlobalRecipeRevision
    ) -> RecipeRevisionView:
        """Materialize one verified global revision in authoritative local rows."""

        actor = _actor(actor)
        clean = self._validated_document(remote.document, slug=remote.slug)
        if recipe_content_sha256(clean) != remote.content_sha256:
            raise CatalogValidationError(
                "global.hash_mismatch", "global recipe content hash is invalid"
            )
        identity = _mapping(clean["identity"])
        if identity != {"publisher": remote.publisher, "slug": remote.slug}:
            raise CatalogValidationError(
                "global.identity_mismatch", "global recipe identity is inconsistent"
            )
        with self._sessions.begin() as session:
            imported = session.scalar(
                select(RecipeImport).where(
                    RecipeImport.source_kind == "global",
                    RecipeImport.source_sha256 == remote.content_sha256,
                )
            )
            if imported is not None:
                recipe = self._require_recipe(session, imported.recipe_id)
                revision = session.scalar(
                    select(LocalRecipeRevision).where(
                        LocalRecipeRevision.recipe_id == recipe.id,
                        LocalRecipeRevision.content_sha256 == remote.content_sha256,
                    )
                )
                if revision is None:
                    raise CatalogConflict(
                        "global.history_inconsistent", "local import history is inconsistent"
                    )
                return _view(recipe, revision)

            link = session.scalar(
                select(RecipeGlobalLink).where(
                    RecipeGlobalLink.global_publisher == remote.publisher,
                    RecipeGlobalLink.global_slug == remote.slug,
                )
            )
            metadata = _mapping(clean["metadata"])
            now = self._clock()
            if link is None:
                if self._repository.recipe_by_slug(session, remote.slug) is not None:
                    raise CatalogConflict(
                        "global.slug_conflict",
                        "a different local recipe already uses this slug; fork or rename it",
                    )
                recipe = LocalRecipe(
                    slug=remote.slug,
                    title=str(metadata["title"]),
                    description=str(metadata["description"]),
                    source_kind="global",
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(recipe)
                session.flush()
                local_number = 1
            else:
                recipe = self._require_recipe(session, link.recipe_id, for_update=True)
                if remote.revision_number <= link.global_revision:
                    raise CatalogConflict(
                        "global.revision_stale",
                        "requested global revision is older than the local imported revision",
                    )
                local_number = self._repository.next_revision_number(session, recipe.id)
                recipe.title = str(metadata["title"])
                recipe.description = str(metadata["description"])
                recipe.updated_at = now

            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=local_number,
                lifecycle="resolved",
                schema_version=1,
                document=clean,
                content_sha256=remote.content_sha256,
                created_by=actor,
                created_at=now,
            )
            session.add(revision)
            session.flush()
            session.add(
                RecipeImport(
                    recipe_id=recipe.id,
                    source_kind="global",
                    source_reference=remote.uri,
                    source_sha256=remote.content_sha256,
                    redacted_source={
                        "publisher": remote.publisher,
                        "slug": remote.slug,
                        "recipe_id": remote.recipe_id,
                        "revision_number": remote.revision_number,
                        "revision_id": remote.revision_id,
                        "published_at": remote.published_at,
                    },
                    created_by=actor,
                    created_at=now,
                )
            )
            if link is None:
                link = RecipeGlobalLink(
                    recipe_id=recipe.id,
                    global_recipe_id=remote.recipe_id,
                    global_publisher=remote.publisher,
                    global_slug=remote.slug,
                    global_revision=remote.revision_number,
                    global_content_sha256=remote.content_sha256,
                    sync_state="current",
                    synced_at=now,
                )
                session.add(link)
            else:
                link.global_recipe_id = remote.recipe_id
                link.global_revision = remote.revision_number
                link.global_content_sha256 = remote.content_sha256
                link.sync_state = "current"
                link.synced_at = now
            session.flush()
            return _view(recipe, revision)

    def attach_test_report(
        self, recipe_id: str, report: Mapping[str, object], actor: str
    ) -> dict[str, object]:
        """Validate publisher evidence without claiming Vonk certification."""

        actor = _actor(actor)
        sensitive = sensitive_document_path(report)
        if sensitive is not None:
            raise CatalogValidationError(
                "catalog.sensitive_field", f"sensitive field is forbidden at {sensitive}"
            )
        clean: dict[str, object] = copy.deepcopy(dict(report))
        errors = sorted(
            _test_report_validator().iter_errors(clean),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            path = ".".join(map(str, errors[0].absolute_path)) or "$"
            raise CatalogValidationError(
                "catalog.test_report_invalid", f"test report is invalid at {path}"
            )
        with self._sessions.begin() as session:
            recipe = self._require_recipe(session, recipe_id)
            revision = self._repository.latest_revision(session, recipe.id)
            if revision is None or revision.lifecycle != "resolved" or revision.content_sha256 is None:
                raise CatalogConflict(
                    "catalog.recipe_unresolved", "resolve the recipe before attaching a test report"
                )
            runtime = _mapping(revision.document["runtime"])
            image = str(runtime["image"])
            image_digest = "sha256:" + image.rsplit("@sha256:", 1)[-1]
            topology = _mapping(revision.document["topology"])
            tested = topology.get("tested_node_counts")
            by_name = {
                str(item.get("name")): item.get("passed")
                for item in clean["checks"]
                if isinstance(item, Mapping)
            }
            if clean.get("recipe_sha256") != revision.content_sha256:
                raise CatalogValidationError(
                    "catalog.test_report_recipe_mismatch",
                    "test report does not match this recipe revision",
                )
            if clean.get("image_digest") != image_digest:
                raise CatalogValidationError(
                    "catalog.test_report_image_mismatch",
                    "test report does not match this runtime image",
                )
            if not isinstance(tested, list) or clean.get("node_count") not in tested:
                raise CatalogValidationError(
                    "catalog.test_report_topology_mismatch",
                    "test report node count is not declared as tested",
                )
            if any(by_name.get(name) is not True for name in _REQUIRED_TEST_CHECKS):
                raise CatalogValidationError(
                    "catalog.test_report_failed",
                    "test report must show all required lifecycle and inference checks passed",
                )
            try:
                started = datetime.fromisoformat(str(clean["started_at"])).astimezone(UTC)
                finished = datetime.fromisoformat(str(clean["finished_at"])).astimezone(UTC)
            except ValueError as error:
                raise CatalogValidationError(
                    "catalog.test_report_timestamps",
                    "test report timestamps are invalid",
                ) from error
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            else:
                now = now.astimezone(UTC)
            if not (
                started <= finished
                and finished - started <= timedelta(hours=24)
                and now - timedelta(days=90) <= finished <= now + timedelta(minutes=5)
            ):
                raise CatalogValidationError(
                    "catalog.test_report_timestamps",
                    "test report timestamps must be ordered, recent, and within 24 hours",
                )
            encoded = json.dumps(
                clean, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            existing = session.scalar(
                select(RecipeTestReport).where(
                    RecipeTestReport.recipe_revision_id == revision.id,
                    RecipeTestReport.report_sha256 == digest,
                )
            )
            if existing is None:
                session.add(
                    RecipeTestReport(
                        recipe_revision_id=revision.id,
                        report_sha256=digest,
                        report=clean,
                        created_by=actor,
                        created_at=self._clock(),
                    )
                )
            return copy.deepcopy(clean)

    def publication_export(
        self, recipe_id: str, target_publisher: str
    ) -> dict[str, object]:
        if not _SLUG.fullmatch(target_publisher):
            raise CatalogValidationError(
                "catalog.publisher", "target publisher namespace is invalid"
            )
        with self._sessions() as session:
            recipe = self._require_recipe(session, recipe_id)
            revision = self._repository.latest_revision(session, recipe.id)
            if revision is None or revision.lifecycle != "resolved" or revision.content_sha256 is None:
                raise CatalogConflict(
                    "catalog.recipe_unresolved", "resolve the recipe before exporting it"
                )
            evidence = session.scalar(
                select(RecipeTestReport)
                .where(RecipeTestReport.recipe_revision_id == revision.id)
                .order_by(RecipeTestReport.created_at.desc(), RecipeTestReport.id.desc())
                .limit(1)
            )
            if evidence is None:
                raise CatalogConflict(
                    "catalog.test_report_required",
                    "attach a passing local test report before publication export",
                )
            document = copy.deepcopy(revision.document)
            report = copy.deepcopy(evidence.report)
        identity = _mapping(document["identity"])
        identity["publisher"] = target_publisher
        validate_recipe(document)
        report["recipe_sha256"] = recipe_content_sha256(document)
        return {"recipe": document, "test_report": report}

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
        artifacts = clean.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                revision = str(artifact.get("revision", "")).lower()
                if revision in _MUTABLE_REVISIONS or revision.endswith("-latest"):
                    raise CatalogValidationError(
                        "catalog.mutable_artifact", "artifact revision must be immutable"
                    )
        try:
            validate_recipe(clean)
        except RecipeContractError as error:
            raise CatalogValidationError(error.code, f"{error.path}: {error.detail}") from error
        identity = _mapping(clean["identity"])
        if identity.get("slug") != slug:
            raise CatalogValidationError(
                "catalog.slug_mismatch", "recipe identity slug does not match"
            )
        return clean


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogValidationError("catalog.document", "recipe object is invalid")
    return value


@lru_cache(maxsize=1)
def _test_report_validator() -> Draft202012Validator:
    root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (root / "schemas/global/test-report-v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


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
