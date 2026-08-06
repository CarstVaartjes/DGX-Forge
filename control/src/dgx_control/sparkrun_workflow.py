"""Preview and transactionally persist explainable SparkRun imports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import LocalRecipe, LocalRecipeRevision, RecipeImport, RecipeImportItem
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


class SparkRunWorkflow:
    def __init__(self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime]) -> None:
        self._sessions, self._clock = sessions, clock

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
