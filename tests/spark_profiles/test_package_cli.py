from __future__ import annotations

import json
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from spark_profiles.cli import main
from spark_profiles.control_client import (
    ControlClient,
    PackagePlanResponse,
    PackageProgressResponse,
    PackagePromotionResponse,
)

CANDIDATE = "a" * 64
PREVIEW = "sha256:" + "b" * 64
PLAN = "sha256:" + "c" * 64
RELEASE = "sha256:" + "d" * 64
REQUEST_ID = "20000000-0000-4000-8000-000000000002"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def package_candidates(self, family_id: str | None = None, *, cursor: str | None = None, limit: int = 20):
        self.calls.append(("candidates", family_id, cursor, limit))
        return {"candidates": []}

    def package_families(self, *, cursor: str | None = None, limit: int = 20):
        self.calls.append(("families", cursor, limit))
        return {"families": []}

    def package_candidate(self, candidate_id: str):
        self.calls.append(("candidate", candidate_id))
        return {"id": candidate_id}

    def package_resolution(self, candidate_id: str):
        self.calls.append(("resolution", candidate_id))
        return {"candidate_id": candidate_id}

    def package_compatibility(self, candidate_id: str):
        self.calls.append(("compatibility", candidate_id))
        return {"candidate_id": candidate_id}

    def preview_package_promotion(self, candidate_id: str) -> PackagePlanResponse:
        self.calls.append(("preview", candidate_id))
        return PackagePlanResponse.from_dict({"candidate_id": candidate_id, "release_digest": RELEASE, "digest": PREVIEW, "state": "ready"})

    def promote_package(self, candidate_id: str, preview_digest: str, *, request_id: str) -> PackagePromotionResponse:
        self.calls.append(("promote", candidate_id, preview_digest, request_id))
        return PackagePromotionResponse.from_dict({"candidate_id": candidate_id, "release_digest": RELEASE, "digest": preview_digest, "state": "promoted"})

    def preview_deployment_rollout(self, deployment_id: str) -> PackagePlanResponse:
        self.calls.append(("rollout-preview", deployment_id))
        return PackagePlanResponse.from_dict({"deployment_id": deployment_id, "release_digest": RELEASE, "digest": PLAN, "state": "ready"})

    def rollout_deployment(self, deployment_id: str, plan_digest: str, *, request_id: str):
        self.calls.append(("rollout", deployment_id, plan_digest, request_id))
        return {"id": "10000000-0000-4000-8000-000000000001", "state": "planned", "plan_digest": plan_digest, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 1}}

    def package_deployments(self, *, cursor: str | None = None, limit: int = 20):
        self.calls.append(("deployments", cursor, limit))
        return {"deployments": []}

    def package_deployment(self, deployment_id: str):
        self.calls.append(("deployment", deployment_id))
        return {"id": deployment_id}

    def package_rollout(self, deployment_id: str, rollout_id: str) -> PackageProgressResponse:
        self.calls.append(("status", deployment_id, rollout_id))
        return PackageProgressResponse.from_dict({"id": rollout_id, "state": "planned", "plan_digest": PLAN, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 1}})

    def preview_package_validation(self, candidate_id: str) -> PackagePlanResponse:
        self.calls.append(("validation-preview", candidate_id))
        return self.preview_package_promotion(candidate_id)

    def validate_package(self, candidate_id: str, plan_digest: str, *, request_id: str) -> PackageProgressResponse:
        self.calls.append(("validate", candidate_id, plan_digest, request_id))
        return PackageProgressResponse.from_dict({"id": "10000000-0000-4000-8000-000000000001", "state": "planned", "plan_digest": plan_digest, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 1}})

    def package_validation(self, validation_id: str) -> PackageProgressResponse:
        self.calls.append(("validation", validation_id))
        return PackageProgressResponse.from_dict({"id": validation_id, "state": "planned", "plan_digest": PREVIEW, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 1}})

    def preview_package_gc(self) -> PackagePlanResponse:
        self.calls.append(("gc-preview",))
        return PackagePlanResponse.from_dict({"digest": PLAN, "state": "ready", "reclaim_bytes": 0})

    def apply_package_gc(self, plan_digest: str, *, request_id: str) -> PackageProgressResponse:
        self.calls.append(("gc", plan_digest, request_id))
        return self.validate_package(CANDIDATE, plan_digest, request_id=request_id)

    def preview_deployment_repair(self, deployment_id: str) -> PackagePlanResponse:
        self.calls.append(("repair-preview", deployment_id))
        return self.preview_deployment_rollout(deployment_id)

    def repair_deployment(self, deployment_id: str, plan_digest: str, *, request_id: str) -> PackageProgressResponse:
        self.calls.append(("repair", deployment_id, plan_digest, request_id))
        return self.validate_package(CANDIDATE, plan_digest, request_id=request_id)


def _invoke(client: object, *arguments: str) -> tuple[int, object]:
    stdout = StringIO()
    with redirect_stdout(stdout), redirect_stderr(StringIO()):
        result = main(arguments, control_client=client, request_id_factory=lambda: REQUEST_ID)
    return result, json.loads(stdout.getvalue())


def test_package_and_deployment_commands_preserve_server_authoritative_digests() -> None:
    # Break caught: the CLI reconstructs a digest or sends a different value
    # from the preview/plan the operator explicitly selected.
    client = FakeClient()

    preview_status, preview = _invoke(client, "admin", "packages", "promote-preview", "--candidate", CANDIDATE, "--json")
    promote_status, promotion = _invoke(client, "admin", "packages", "promote", "--candidate", CANDIDATE, "--preview-digest", preview["digest"], "--json")
    rollout_preview_status, rollout_preview = _invoke(client, "admin", "deployments", "rollout-preview", "--deployment", "synthetic-canary", "--json")
    rollout_status, rollout = _invoke(client, "admin", "deployments", "rollout", "--deployment", "synthetic-canary", "--plan-digest", rollout_preview["digest"], "--json")

    assert (preview_status, promote_status, rollout_preview_status, rollout_status) == (0, 0, 0, 0)
    assert promotion["release_digest"] == RELEASE
    assert rollout["plan_digest"] == PLAN
    assert client.calls == [
        ("preview", CANDIDATE),
        ("promote", CANDIDATE, PREVIEW, REQUEST_ID),
        ("rollout-preview", "synthetic-canary"),
        ("rollout", "synthetic-canary", PLAN, REQUEST_ID),
    ]


def test_package_apply_commands_reject_noncanonical_digests_before_mutation() -> None:
    # Break caught: malformed values reach package-promotion or rollout APIs.
    client = FakeClient()

    promotion, _ = _invoke(client, "admin", "packages", "promote", "--candidate", CANDIDATE, "--preview-digest", "not-a-digest", "--json")
    rollout, _ = _invoke(client, "admin", "deployments", "rollout", "--deployment", "synthetic-canary", "--plan-digest", "sha256:" + "A" * 64, "--json")

    assert (promotion, rollout) == (2, 2)
    assert client.calls == []


def test_package_validation_repair_and_gc_apply_only_server_preview_digests() -> None:
    # Break caught: recovery/validation commands bypass their preview boundary
    # or use a locally derived digest for an apply mutation.
    client = FakeClient()

    validation_preview_status, validation_preview = _invoke(client, "admin", "packages", "validation-preview", "--candidate", CANDIDATE, "--json")
    validation_status, validation = _invoke(client, "admin", "packages", "validate", "--candidate", CANDIDATE, "--plan-digest", validation_preview["digest"], "--json")
    repair_preview_status, repair_preview = _invoke(client, "admin", "deployments", "repair-preview", "--deployment", "synthetic-canary", "--json")
    repair_status, repair = _invoke(client, "admin", "deployments", "repair", "--deployment", "synthetic-canary", "--plan-digest", repair_preview["digest"], "--json")
    gc_preview_status, gc_preview = _invoke(client, "admin", "packages", "gc-preview", "--json")
    gc_status, gc = _invoke(client, "admin", "packages", "gc", "--plan-digest", gc_preview["digest"], "--json")

    assert (validation_preview_status, validation_status, repair_preview_status, repair_status, gc_preview_status, gc_status) == (0, 0, 0, 0, 0, 0)
    assert validation["plan_digest"] == PREVIEW
    assert repair["plan_digest"] == PLAN
    assert gc["plan_digest"] == PLAN


class Response:
    def __init__(self, payload: object) -> None:
        self._content = json.dumps(payload).encode()
        self.status = 202
        self.headers = {"content-type": "application/json"}

    def read(self, size: int = -1) -> bytes:
        return self._content if size < 0 else self._content[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_package_client_reuses_a_canonical_caller_request_id_on_mutation(tmp_path) -> None:
    # Break caught: a retry generates a fresh ID or drops the caller's ID from
    # the wire, preventing the server's idempotency boundary from recognizing it.
    token = tmp_path / "token"
    token.write_text("signed-token\n")
    token.chmod(0o600)
    calls: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request, timeout: float) -> Response:
        del timeout
        calls.append(request)
        return Response({"candidate_id": CANDIDATE, "release_digest": RELEASE, "digest": PREVIEW, "state": "promoted"})

    client = ControlClient("https://control.invalid", token, opener=opener)
    first = client.promote_package(CANDIDATE, PREVIEW, request_id=REQUEST_ID)
    replay = client.promote_package(CANDIDATE, PREVIEW, request_id=REQUEST_ID)

    assert first.to_dict() == replay.to_dict()
    assert [call.headers["X-request-id"] for call in calls] == [REQUEST_ID, REQUEST_ID]


def test_package_cli_exposes_every_named_read_projection() -> None:
    # Break caught: an API read capability has no equivalent command, forcing a
    # routine operator to construct private control URLs instead of using CLI.
    client = FakeClient()
    validation_id = "10000000-0000-4000-8000-000000000001"

    results = [
        _invoke(client, "admin", "packages", "families", "--json"),
        _invoke(client, "admin", "packages", "candidates", "get", "--candidate", CANDIDATE, "--json"),
        _invoke(client, "admin", "packages", "candidates", "resolution", "--candidate", CANDIDATE, "--json"),
        _invoke(client, "admin", "packages", "candidates", "compatibility", "--candidate", CANDIDATE, "--json"),
        _invoke(client, "admin", "packages", "validation-status", "--validation", validation_id, "--json"),
        _invoke(client, "admin", "deployments", "list", "--json"),
        _invoke(client, "admin", "deployments", "get", "--deployment", "synthetic-canary", "--json"),
        _invoke(client, "admin", "deployments", "status", "--deployment", "synthetic-canary", "--rollout", validation_id, "--json"),
    ]

    assert [status for status, _payload in results] == [0] * 8
    assert client.calls == [
        ("families", None, 20),
        ("candidate", CANDIDATE),
        ("resolution", CANDIDATE),
        ("compatibility", CANDIDATE),
        ("validation", validation_id),
        ("deployments", None, 20),
        ("deployment", "synthetic-canary"),
        ("status", "synthetic-canary", validation_id),
    ]
