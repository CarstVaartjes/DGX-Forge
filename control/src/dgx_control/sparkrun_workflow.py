"""Preview and transactionally persist explainable SparkRun imports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .import_report import ImportDisposition, ImportReportItem
from .import_resolution import resolve_import
from .model_resolution import ModelTransport
from .models import LocalRecipe, LocalRecipeRevision, RecipeImport, RecipeImportItem
from .recipe_contract import recipe_content_sha256
from .registry_resolution import RegistryTransport
from .sparkrun_importer import SparkRunImportResult, import_sparkrun
from .sparkrun_source import parse_sparkrun_yaml


class SparkRunWorkflowError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class AppliedSparkRunImport:
    import_id: str
    recipe_id: str
    revision_id: str
    revision_number: int
    lifecycle: str
    source_sha256: str
    report_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedSparkRunImport:
    recipe_id: str
    revision_id: str
    revision_number: int
    content_sha256: str


class SparkRunWorkflow:
    def __init__(self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime], registry: RegistryTransport | None = None, models: ModelTransport | None = None) -> None:
        self._sessions, self._clock, self._registry, self._models = sessions, clock, registry, models

    def preview(self, raw: bytes) -> SparkRunImportResult:
        return import_sparkrun(parse_sparkrun_yaml(raw))

    def apply(
        self,
        raw: bytes,
        *,
        source_sha256: str,
        report_digest: str,
        actor: str,
    ) -> AppliedSparkRunImport:
        preview = self.preview(raw)
        if preview.source_sha256 != source_sha256 or preview.report_digest != report_digest:
            raise SparkRunWorkflowError("sparkrun.stale_preview", "SparkRun preview identity changed")
        with self._sessions.begin() as session:
            existing = session.scalar(select(RecipeImport).where(RecipeImport.source_kind == "sparkrun", RecipeImport.source_sha256 == source_sha256))
            if existing is not None:
                revision = session.scalar(select(LocalRecipeRevision).where(LocalRecipeRevision.recipe_id == existing.recipe_id).order_by(LocalRecipeRevision.revision_number.desc()).limit(1))
                assert revision is not None
                return AppliedSparkRunImport(existing.id, existing.recipe_id, revision.id, revision.revision_number, revision.lifecycle, source_sha256, report_digest)
            document = preview.draft_document
            identity, metadata = document["identity"], document["metadata"]
            assert isinstance(identity, dict) and isinstance(metadata, dict)
            now = self._clock()
            recipe = LocalRecipe(slug=str(identity["slug"]), title=str(metadata["title"]), description=str(metadata["description"]), source_kind="sparkrun", created_by=actor, created_at=now, updated_at=now)
            session.add(recipe); session.flush()
            revision = LocalRecipeRevision(recipe_id=recipe.id, revision_number=1, lifecycle="blocked", schema_version=1, document=document, content_sha256=None, created_by=actor, created_at=now)
            session.add(revision)
            imported = RecipeImport(recipe_id=recipe.id, source_kind="sparkrun", source_reference=f"sparkrun:sha256:{source_sha256}", source_sha256=source_sha256, redacted_source=preview.redacted_source, created_by=actor, created_at=now)
            session.add(imported); session.flush()
            session.add_all([RecipeImportItem(import_id=imported.id, source_path=item.source_path, disposition=item.disposition.value, destination_path=item.destination_path, reason_code=item.reason_code, detail=item.detail, blocking=item.blocking) for item in preview.report])
            session.flush()
            return AppliedSparkRunImport(imported.id, recipe.id, revision.id, 1, "blocked", source_sha256, report_digest)

    def resolve(self, recipe_id: str, *, expected_revision: int, overlays: dict[str, object], actor: str) -> ResolvedSparkRunImport:
        if self._registry is None or self._models is None:
            raise SparkRunWorkflowError("sparkrun.resolution_unavailable", "external metadata resolution is unavailable")
        with self._sessions() as session:
            recipe = session.get(LocalRecipe, recipe_id)
            imported = session.scalar(select(RecipeImport).where(RecipeImport.recipe_id == recipe_id))
            revision = session.scalar(select(LocalRecipeRevision).where(LocalRecipeRevision.recipe_id == recipe_id).order_by(LocalRecipeRevision.revision_number.desc()).limit(1))
            if recipe is None or imported is None or revision is None: raise KeyError(recipe_id)
            if recipe.source_kind != "sparkrun" or revision.revision_number != expected_revision or revision.lifecycle not in {"blocked", "draft"}:
                raise SparkRunWorkflowError("catalog.stale_revision", "SparkRun draft revision changed")
            rows = session.scalars(select(RecipeImportItem).where(RecipeImportItem.import_id == imported.id)).all()
            report = tuple(ImportReportItem(row.source_path, ImportDisposition(row.disposition), row.destination_path, row.reason_code, row.detail, row.blocking) for row in rows)
            snapshot_id, snapshot_document = revision.id, revision.document
            imported_result = SparkRunImportResult(snapshot_document, report, imported.source_sha256, "", imported.redacted_source, False)
        resolved = resolve_import(imported_result, overlays, registry=self._registry, models=self._models)
        if not resolved.runnable:
            codes = ", ".join(item.reason_code for item in resolved.blockers[:5])
            raise SparkRunWorkflowError("sparkrun.import_blocked", f"SparkRun import remains blocked: {codes}")
        digest = recipe_content_sha256(resolved.document); now = self._clock()
        with self._sessions.begin() as session:
            current_recipe = session.scalar(select(LocalRecipe).where(LocalRecipe.id == recipe_id).with_for_update())
            current = session.scalar(select(LocalRecipeRevision).where(LocalRecipeRevision.recipe_id == recipe_id).order_by(LocalRecipeRevision.revision_number.desc()).limit(1))
            if current_recipe is None or current is None or current.id != snapshot_id:
                raise SparkRunWorkflowError("catalog.stale_revision", "SparkRun draft revision changed")
            next_revision = LocalRecipeRevision(recipe_id=recipe_id, revision_number=expected_revision + 1, lifecycle="resolved", schema_version=1, document=resolved.document, content_sha256=digest, created_by=actor, created_at=now)
            session.add(next_revision)
            by_path = {item.source_path: item for item in resolved.report}
            for row in session.scalars(select(RecipeImportItem).where(RecipeImportItem.import_id == imported.id)):
                item = by_path[row.source_path]; row.disposition = item.disposition.value; row.reason_code = item.reason_code; row.detail = item.detail; row.blocking = item.blocking; row.destination_path = item.destination_path
            session.flush()
            return ResolvedSparkRunImport(recipe_id, next_revision.id, next_revision.revision_number, digest)
