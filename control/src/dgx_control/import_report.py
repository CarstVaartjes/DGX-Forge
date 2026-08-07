"""Exhaustive one-disposition accounting for recipe imports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ImportReportError(RuntimeError):
    pass


class ImportDisposition(str, Enum):
    IMPORTED = "imported"
    TRANSFORMED = "transformed"
    RESOLUTION_REQUIRED = "resolution_required"
    OVERLAY_REQUIRED = "overlay_required"
    UNSUPPORTED_BLOCKING = "unsupported_blocking"
    DROPPED_REDUNDANT = "dropped_redundant"


@dataclass(frozen=True, slots=True)
class ImportReportItem:
    source_path: str
    disposition: ImportDisposition
    destination_path: str | None
    reason_code: str
    detail: str
    blocking: bool


class ImportReportBuilder:
    def __init__(self, source_paths: tuple[str, ...]) -> None:
        if not source_paths or len(source_paths) != len(set(source_paths)):
            raise ImportReportError("source leaf paths must be unique and non-empty")
        self._source_paths = frozenset(source_paths)
        self._items: dict[str, ImportReportItem] = {}
        self._destinations: set[str] = set()

    def record(
        self,
        source_path: str,
        disposition: ImportDisposition,
        destination_path: str | None,
        reason_code: str,
        detail: str,
        blocking: bool,
    ) -> None:
        synthetic = source_path.startswith("/@missing/")
        if (source_path not in self._source_paths and not synthetic) or source_path in self._items:
            raise ImportReportError(f"source path was not registered exactly once: {source_path}")
        if destination_path is not None:
            if destination_path in self._destinations:
                raise ImportReportError(f"destination path was written twice: {destination_path}")
            self._destinations.add(destination_path)
        if not reason_code or not detail or len(reason_code) > 128 or len(detail) > 1024:
            raise ImportReportError("report explanation is invalid")
        self._items[source_path] = ImportReportItem(
            source_path, disposition, destination_path, reason_code, detail, blocking
        )

    def finalize(self) -> tuple[ImportReportItem, ...]:
        missing = self._source_paths - self._items.keys()
        if missing:
            raise ImportReportError(f"unaccounted source field: {min(missing)}")
        return tuple(self._items[path] for path in sorted(self._items))
