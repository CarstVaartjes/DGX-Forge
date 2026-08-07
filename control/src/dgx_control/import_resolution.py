"""Resolve external identities and typed overlays for one SparkRun import."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass

from .import_report import ImportDisposition, ImportReportItem
from .model_resolution import ModelTransport, resolve_huggingface_snapshot
from .recipe_contract import RecipeContractError, validate_recipe
from .registry_resolution import RegistryTransport, resolve_public_image
from .sparkrun_importer import SparkRunImportResult


class ImportResolutionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    document: dict[str, object]
    report: tuple[ImportReportItem, ...]
    blockers: tuple[ImportReportItem, ...]
    runnable: bool


def resolve_import(
    imported: SparkRunImportResult,
    overlays: Mapping[str, object],
    *,
    registry: RegistryTransport,
    models: ModelTransport,
) -> ResolutionResult:
    document = copy.deepcopy(imported.draft_document)
    runtime = _mapping(document["runtime"])
    image = resolve_public_image(str(runtime["image"]), registry)
    runtime["image"] = image.reference
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ImportResolutionError("import.artifact_shape", "import artifact shape is invalid")
    artifact = _mapping(artifacts[0])
    snapshot = resolve_huggingface_snapshot(str(artifact["repository"]), str(artifact["revision"]), models)
    artifact["expected_bytes"] = snapshot.expected_bytes
    resources = overlays.get("resources")
    if not isinstance(resources, Mapping):
        raise ImportResolutionError("import.resources_required", "resource overlay is required")
    required = ("download_bytes", "installed_bytes", "staging_bytes", "resident_memory_bytes", "activation_memory_bytes")
    normalized_resources: dict[str, int] = {}
    for key in required:
        value = resources.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ImportResolutionError("import.resources_invalid", f"resource overlay {key} is invalid")
        normalized_resources[key] = value
    document["resources"] = {"per_node": normalized_resources, "measurement": "derived"}
    if overlays.get("security_acknowledged") is not True:
        raise ImportResolutionError("import.security_required", "security overlay acknowledgement is required")
    topology = _mapping(document["topology"])
    if topology.get("kind") == "gang":
        supplied = overlays.get("topology")
        if not isinstance(supplied, Mapping):
            raise ImportResolutionError("import.topology_required", "multi-node topology overlay is required")
        topology["fabric"] = copy.deepcopy(supplied.get("fabric"))
        topology["ranks"] = copy.deepcopy(supplied.get("ranks"))
    resolved_items: list[ImportReportItem] = []
    for item in imported.report:
        handled = (
            item.source_path in {"/container", "/model_revision", "/@missing/resources", "/@missing/security", "/@missing/topology-fabric"}
            or item.reason_code == "runtime.environment_review"
        )
        if handled and item.disposition in {ImportDisposition.RESOLUTION_REQUIRED, ImportDisposition.OVERLAY_REQUIRED}:
            resolved_items.append(ImportReportItem(item.source_path, ImportDisposition.TRANSFORMED, item.destination_path, f"{item.reason_code}.resolved", f"Resolved: {item.detail}", False))
        else:
            resolved_items.append(item)
    blockers = tuple(item for item in resolved_items if item.blocking or item.disposition in {ImportDisposition.RESOLUTION_REQUIRED, ImportDisposition.OVERLAY_REQUIRED, ImportDisposition.UNSUPPORTED_BLOCKING})
    if not blockers:
        try:
            validate_recipe(document)
        except RecipeContractError as error:
            raise ImportResolutionError(error.code, f"{error.path}: {error.detail}") from error
    return ResolutionResult(document, tuple(resolved_items), blockers, not blockers)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ImportResolutionError("import.document_invalid", "import document is invalid")
    return value
