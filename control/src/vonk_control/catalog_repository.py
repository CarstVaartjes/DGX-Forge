"""Transactional persistence helpers for the local recipe catalog."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import LocalRecipe, LocalRecipeRevision

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:authorization|credential|password|secret|token|private_key|certificate)(?:$|_)",
    re.IGNORECASE,
)


class CatalogRepository:
    """Keep row-locking and source redaction rules in one small boundary."""

    def recipe(
        self, session: Session, recipe_id: str, *, for_update: bool = False
    ) -> LocalRecipe | None:
        statement = select(LocalRecipe).where(LocalRecipe.id == recipe_id)
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def recipe_by_slug(
        self, session: Session, slug: str, *, for_update: bool = False
    ) -> LocalRecipe | None:
        statement = select(LocalRecipe).where(LocalRecipe.slug == slug)
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def latest_revision(
        self, session: Session, recipe_id: str
    ) -> LocalRecipeRevision | None:
        return session.scalar(
            select(LocalRecipeRevision)
            .where(LocalRecipeRevision.recipe_id == recipe_id)
            .order_by(LocalRecipeRevision.revision_number.desc())
            .limit(1)
        )

    def revision(
        self, session: Session, recipe_id: str, revision_number: int
    ) -> LocalRecipeRevision | None:
        return session.scalar(
            select(LocalRecipeRevision).where(
                LocalRecipeRevision.recipe_id == recipe_id,
                LocalRecipeRevision.revision_number == revision_number,
            )
        )

    def next_revision_number(self, session: Session, recipe_id: str) -> int:
        # Locking the stable parent serializes revision allocation on PostgreSQL.
        if self.recipe(session, recipe_id, for_update=True) is None:
            raise KeyError(recipe_id)
        current = session.scalar(
            select(func.max(LocalRecipeRevision.revision_number)).where(
                LocalRecipeRevision.recipe_id == recipe_id
            )
        )
        return int(current or 0) + 1

    def redact_source(self, source: object) -> object:
        def redact(value: object) -> object:
            if isinstance(value, Mapping):
                return {
                    str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(child)
                    for key, child in value.items()
                }
            if isinstance(value, list):
                return [redact(child) for child in value]
            return copy.deepcopy(value)

        return redact(source)


def sensitive_document_path(document: object) -> str | None:
    """Return only the sensitive key path; values must never enter errors/logs."""

    def inspect(value: object, path: str) -> str | None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}"
                if _SENSITIVE_KEY.search(key_text):
                    return child_path
                found = inspect(child, child_path)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found = inspect(child, f"{path}[{index}]")
                if found is not None:
                    return found
        return None

    return inspect(document, "$")
