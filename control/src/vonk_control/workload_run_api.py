"""Authenticated preview/apply API for WorkloadRun imports."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .audit import AuditRecord
from .auth import Actor
from .workload_run_source import WorkloadRunParseError
from .workload_run_workflow import WorkloadRunWorkflow, WorkloadRunWorkflowError


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewRequest(StrictModel):
    source_yaml: str = Field(min_length=1, max_length=262144)


class ApplyRequest(PreviewRequest):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResolveImportRequest(StrictModel):
    expected_revision: int = Field(ge=1, strict=True)
    overlays: dict[str, object]


def install_workload_run_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    audits: AuditSink,
    workflow: WorkloadRunWorkflow | None,
) -> None:
    authenticated = actor_dependency
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(
        {
            (
                "post",
                "/api/v1/catalog/imports/workload_run/preview",
            ): "previewWorkloadRunImport",
            ("post", "/api/v1/catalog/imports/workload_run"): "applyWorkloadRunImport",
            (
                "post",
                "/api/v1/catalog/recipes/{recipe_id}/resolve-import",
            ): "resolveWorkloadRunImport",
        }
    )

    def service() -> WorkloadRunWorkflow:
        if workflow is None:
            raise HTTPException(status_code=503, detail="WorkloadRun import unavailable")
        return workflow

    def admin(actor: Actor) -> None:
        if actor.role != "administrator":
            raise HTTPException(status_code=403, detail="insufficient role")

    def problem(
        request: Request, code: str, detail: str, status_code: int
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "code": code,
                "detail": detail[:256],
                "request_id": request.state.request_id,
            },
        )

    def preview_document(result):
        return {
            "draft_document": result.draft_document,
            "source_bundle": {
                "sha256": result.bundle.sha256,
                "archive_bytes": len(result.bundle.archive),
                "files": [asdict(item) for item in result.bundle.manifest.files],
            },
            "report": [
                {**asdict(item), "disposition": item.disposition.value}
                for item in result.report
            ],
            "source_sha256": result.source_sha256,
            "report_digest": result.report_digest,
            "redacted_source": result.redacted_source,
            "runnable": result.runnable,
        }

    @app.post(
        "/api/v1/catalog/imports/workload_run/preview", operation_id="previewWorkloadRunImport"
    )
    def preview(body: PreviewRequest, request: Request, actor: Actor = authenticated):
        admin(actor)
        try:
            return preview_document(service().preview(body.source_yaml.encode()))
        except WorkloadRunParseError as error:
            return problem(request, "workload_run.invalid_source", str(error), 422)

    @app.post(
        "/api/v1/catalog/imports/workload_run",
        status_code=status.HTTP_201_CREATED,
        operation_id="applyWorkloadRunImport",
    )
    def apply(body: ApplyRequest, request: Request, actor: Actor = authenticated):
        admin(actor)
        try:
            result = service().apply(
                body.source_yaml.encode(),
                source_sha256=body.source_sha256,
                report_digest=body.report_digest,
                actor=actor.subject,
            )
        except WorkloadRunWorkflowError as error:
            return problem(request, error.code, str(error), 409)
        except WorkloadRunParseError as error:
            return problem(request, "workload_run.invalid_source", str(error), 422)
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.workload_run.import",
                None,
                (result.import_id, result.recipe_id, result.source_sha256),
            )
        )
        return asdict(result)

    @app.post(
        "/api/v1/catalog/recipes/{recipe_id}/resolve-import",
        operation_id="resolveWorkloadRunImport",
    )
    def resolve_imported(
        body: ResolveImportRequest,
        request: Request,
        recipe_id: str,
        actor: Actor = authenticated,
    ):
        admin(actor)
        try:
            result = service().resolve(
                recipe_id,
                expected_revision=body.expected_revision,
                overlays=body.overlays,
                actor=actor.subject,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="recipe not found") from None
        except WorkloadRunWorkflowError as error:
            return problem(
                request,
                error.code,
                str(error),
                409
                if error.code in {"catalog.stale_revision", "workload_run.import_blocked"}
                else 503,
            )
        audits.append(
            AuditRecord(
                request.state.request_id,
                actor.subject,
                "catalog.workload_run.resolve",
                None,
                (result.recipe_id, result.revision_id, result.content_sha256),
            )
        )
        return asdict(result)
