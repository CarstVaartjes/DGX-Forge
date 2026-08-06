"""Translate parsed SparkRun sources into safe, explainable local drafts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .import_report import (
    ImportDisposition,
    ImportReportBuilder,
    ImportReportItem,
)
from .sparkrun_source import SparkRunSource
from .runtime_compilers import RuntimeCompileError, RuntimeProjection, compile_runtime

_SENSITIVE = re.compile(
    r"(?:^|_)(?:authorization|credential|password|secret|token|private_key|certificate)(?:$|_)",
    re.IGNORECASE,
)
_DIGEST_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
_MUTABLE_REVISIONS = frozenset({"main", "master", "latest", "head"})


@dataclass(frozen=True, slots=True)
class SparkRunImportResult:
    draft_document: dict[str, object]
    report: tuple[ImportReportItem, ...]
    source_sha256: str
    report_digest: str
    redacted_source: dict[str, object]
    runnable: bool


def import_sparkrun(source: SparkRunSource) -> SparkRunImportResult:
    builder = ImportReportBuilder(source.leaf_paths())
    projection: RuntimeProjection | None = None
    compiler_error: str | None = None
    try:
        projection = compile_runtime(source, builder)
    except RuntimeCompileError as error:
        compiler_error = str(error)[:240]
    for path in source.leaf_paths():
        _classify(source, builder, path, compiler_error=compiler_error)
    builder.record(
        "/@missing/resources",
        ImportDisposition.OVERLAY_REQUIRED,
        None,
        "resources.overlay_required",
        "SparkRun does not declare a complete download, install, staging, resident-memory, and activation-memory envelope. Enter measured or verified byte values.",
        True,
    )
    builder.record(
        "/@missing/security",
        ImportDisposition.OVERLAY_REQUIRED,
        None,
        "security.overlay_required",
        "Confirm the unprivileged GPU device and read-only model mount policy before this recipe can run.",
        True,
    )
    if (source.min_nodes or 1) > 1:
        builder.record(
            "/@missing/topology-fabric",
            ImportDisposition.OVERLAY_REQUIRED,
            None,
            "topology.fabric_required",
            "Multi-node imports require explicit ranks, transport, and minimum fabric bandwidth.",
            True,
        )
    report = builder.finalize()
    report_document = [
        {**asdict(item), "disposition": item.disposition.value} for item in report
    ]
    report_digest = hashlib.sha256(
        json.dumps(report_document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    runnable = not any(
        item.disposition
        in {
            ImportDisposition.RESOLUTION_REQUIRED,
            ImportDisposition.OVERLAY_REQUIRED,
            ImportDisposition.UNSUPPORTED_BLOCKING,
        }
        or item.blocking
        for item in report
    )
    return SparkRunImportResult(
        draft_document=_draft(source, projection),
        report=report,
        source_sha256=source.source_sha256,
        report_digest=report_digest,
        redacted_source=_redact(source.document),
        runnable=runnable,
    )


def _classify(
    source: SparkRunSource,
    builder: ImportReportBuilder,
    path: str,
    *,
    compiler_error: str | None,
) -> None:
    top = path.split("/", 2)[1] if path.startswith("/") else ""
    destination: str | None = None
    if top == "recipe_version":
        disposition, reason, detail, blocking = ImportDisposition.DROPPED_REDUNDANT, "schema.normalized", "The source schema marker is replaced by Vonk recipe schema version 1.", False
    elif top == "model":
        disposition, destination, reason, detail, blocking = ImportDisposition.IMPORTED, "/artifacts/0/repository", "artifact.repository", "The model repository is imported as an external artifact identity.", False
    elif top == "model_revision":
        mutable = source.model_revision is None or source.model_revision.lower() in _MUTABLE_REVISIONS
        disposition = ImportDisposition.RESOLUTION_REQUIRED if mutable else ImportDisposition.IMPORTED
        destination, reason, detail, blocking = "/artifacts/0/revision", "artifact.revision", "The model revision must resolve to an immutable provider revision." if mutable else "The immutable model revision is imported.", mutable
    elif top == "runtime":
        disposition, destination, reason, detail, blocking = ImportDisposition.TRANSFORMED, "/runtime/family", "runtime.family", "The SparkRun runtime name is normalized to a typed Vonk runtime family.", False
    elif top == "container":
        immutable = source.container is not None and _DIGEST_IMAGE.fullmatch(source.container) is not None
        disposition = ImportDisposition.IMPORTED if immutable else ImportDisposition.RESOLUTION_REQUIRED
        destination, reason, detail, blocking = "/runtime/image", "runtime.image_digest", "The immutable ARM64 image identity is imported." if immutable else "The container tag must resolve to a linux/arm64 manifest digest before running.", not immutable
    elif top in {"min_nodes", "max_nodes"}:
        disposition, destination, reason, detail, blocking = ImportDisposition.IMPORTED, f"/topology/{top}", "topology.node_bound", "The declared node bound is imported.", False
    elif top == "metadata":
        suffix = path.removeprefix("/metadata")
        disposition, destination, reason, detail, blocking = ImportDisposition.IMPORTED, f"/metadata{suffix}", "metadata.imported", "Recipe metadata is imported.", False
    elif top == "defaults":
        suffix = path.removeprefix("/defaults")
        disposition, destination, reason, detail, blocking = ImportDisposition.TRANSFORMED, f"/runtime/arguments{suffix}", "runtime.default", "The default is available only to the typed runtime compiler.", False
    elif top == "command":
        if compiler_error is None:
            disposition, destination, reason, detail, blocking = ImportDisposition.TRANSFORMED, "/runtime/arguments", "runtime.command", "The command was parsed as an allowlisted runtime grammar; it was never executed as shell text.", False
        else:
            disposition, reason, detail, blocking = ImportDisposition.UNSUPPORTED_BLOCKING, "runtime.command_unsupported", f"The command cannot be represented safely: {compiler_error}", True
    elif top == "env":
        suffix = path.removeprefix("/env")
        disposition, destination, reason, detail, blocking = ImportDisposition.RESOLUTION_REQUIRED, f"/runtime/environment{suffix}", "runtime.environment_review", "This literal environment setting requires runtime-specific review; secret values are never accepted.", True
    elif top in {"mods", "tuning"}:
        disposition, reason, detail, blocking = ImportDisposition.UNSUPPORTED_BLOCKING, f"sparkrun.{top}_unsupported", f"SparkRun {top} cannot execute from a Vonk recipe; replace it with a published digest-pinned container capability.", True
    elif top == "benchmark":
        disposition, reason, detail, blocking = ImportDisposition.DROPPED_REDUNDANT, "benchmark.not_authority", "Benchmark claims are not treated as installation or runtime authority and are not imported.", False
    else:
        disposition, reason, detail, blocking = ImportDisposition.UNSUPPORTED_BLOCKING, "sparkrun.unknown_field", f"Unknown SparkRun field {top!r} is preserved in the report but cannot authorize execution.", True
    builder.record(path, disposition, destination, reason, detail, blocking)


def _draft(
    source: SparkRunSource, projection: RuntimeProjection | None
) -> dict[str, object]:
    slug = re.sub(r"[^a-z0-9-]+", "-", source.model.rsplit("/", 1)[-1].lower()).strip("-")
    if len(slug) < 2:
        slug = f"model-{slug or 'import'}"
    slug = slug[:63].rstrip("-")
    minimum, maximum = source.min_nodes or 1, source.max_nodes or source.min_nodes or 1
    if minimum == maximum == 1:
        topology: dict[str, object] = {"kind": "single", "min_nodes": 1, "max_nodes": 1, "tested_node_counts": [1]}
    else:
        topology = {
            "kind": "gang", "min_nodes": minimum, "max_nodes": maximum,
            "tested_node_counts": [minimum], "fabric": {"transport": "tcp", "minimum_bandwidth_mbps": 1},
            "ranks": [{"rank": rank, "role": "entrypoint" if rank == 0 else "worker"} for rank in range(minimum)],
        }
    return {
        "schema_version": 1,
        "identity": {"publisher": "sparkrun", "slug": slug},
        "metadata": {
            "title": source.metadata.title or source.model.rsplit("/", 1)[-1],
            "description": source.metadata.description or f"Imported SparkRun profile for {source.model}.",
            "tags": list(source.metadata.tags),
        },
        "workload": {"family": slug, "capabilities": ["openai.chat"]},
        "artifacts": [{"kind": "huggingface.snapshot", "repository": source.model, "revision": source.model_revision or "resolution-required", "expected_bytes": 1}],
        "runtime": {
            "family": projection.family if projection is not None else source.runtime,
            "image": source.container or "resolution-required",
            "architecture": "linux/arm64",
            "arguments": projection.recipe_arguments() if projection is not None else [],
        },
        "resources": {"per_node": {"download_bytes": 1, "installed_bytes": 1, "staging_bytes": 1, "resident_memory_bytes": 1, "activation_memory_bytes": 1}, "measurement": "declared"},
        "topology": topology,
        "endpoint": {
            "protocol": "openai",
            "port": int(projection.endpoint["port"]) if projection is not None else 8000,
            "model_aliases": [slug],
            "health_path": str(projection.endpoint["health_path"]) if projection is not None else "/v1/models",
        },
        "security": {"devices": ["nvidia.com/gpu=all"], "capabilities": [], "host_network": False, "privileged": False, "mounts": [{"source": "model", "target": "/models", "read_only": True}]},
        "provenance": {"source_kind": "sparkrun", "source_reference": None, "attribution": [f"Imported from SparkRun profile sha256:{source.source_sha256}"]},
    }


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {key: "<redacted>" if _SENSITIVE.search(key) else _redact(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return copy.deepcopy(value)
