from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from dgx_control.api import create_app
from dgx_control.audit import MemoryAuditStore
from dgx_control.auth import Actor, TokenCodec
from dgx_control.install_admission import InstallNodePlan, InstallPlan
from dgx_control.recipe_operations import RecipeOperationView
from dgx_control.run_admission import RunNodePlan, RunPlan


NODE = "spk_" + "1" * 32
REVISION = "00000000-0000-4000-8000-000000000001"
INSTALLATION = "00000000-0000-4000-8000-000000000002"
RUN = "00000000-0000-4000-8000-000000000003"
OPERATION = "00000000-0000-4000-8000-000000000004"
NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Jobs:
    def list(self, **_kwargs): return []
    def get(self, _job_id): raise KeyError


class Recipes:
    def __init__(self) -> None:
        self.install_plan = InstallPlan(
            REVISION,
            "a" * 64,
            True,
            (
                InstallNodePlan(
                    NODE, True, NOW, 1000, 0, 0, 100, 120, 10, 880, (), ()
                ),
            ),
            "b" * 64,
        )
        self.run_plan = RunPlan(
            INSTALLATION,
            REVISION,
            True,
            (
                RunNodePlan(
                    NODE,
                    0,
                    "entrypoint",
                    8000,
                    True,
                    NOW,
                    200,
                    500,
                    500,
                    0,
                    0,
                    300,
                    50,
                    (),
                    (),
                ),
            ),
            "c" * 64,
        )
        self.calls: list[tuple[str, object]] = []

    def preview_install(self, revision, nodes):
        self.calls.append(("preview_install", (revision, nodes)))
        return self.install_plan

    def install(self, plan, **kwargs):
        self.calls.append(("install", kwargs))
        return RecipeOperationView(OPERATION, "recipe.install", INSTALLATION, "running", plan.plan_digest, (NODE,), None)

    def preview_run(self, installation, placements):
        self.calls.append(("preview_run", (installation, placements)))
        return self.run_plan

    def start(self, plan, **kwargs):
        self.calls.append(("start", kwargs))
        return RecipeOperationView(OPERATION, "recipe.start", RUN, "running", plan.plan_digest, (NODE,), None)

    def stop(self, run_id, **kwargs):
        self.calls.append(("stop", (run_id, kwargs)))
        return RecipeOperationView(OPERATION, "recipe.stop", run_id, "running", "c" * 64, (NODE,), None)

    def uninstall(self, installation_id, **kwargs):
        self.calls.append(("uninstall", (installation_id, kwargs)))
        return RecipeOperationView(OPERATION, "recipe.uninstall", installation_id, "running", "b" * 64, (NODE,), None)

    def retry(self, operation_id, **kwargs):
        self.calls.append(("retry", (operation_id, kwargs)))
        return RecipeOperationView(OPERATION, "recipe.install", INSTALLATION, "running", "b" * 64, (NODE,), None)

    def get(self, operation_id):
        self.calls.append(("get", operation_id))
        return RecipeOperationView(operation_id, "recipe.install", INSTALLATION, "succeeded", "b" * 64, (NODE,), {"successful_nodes": [NODE]})


def setup():
    codec = TokenCodec(b"r" * 32)
    audits = MemoryAuditStore()
    recipes = Recipes()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        recipe_operations=recipes,
    )

    def headers(role="administrator"):
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, recipes, audits


def test_preview_install_and_run_expose_exact_capacity_math() -> None:
    client, headers, _recipes, _audits = setup()
    install = client.post(
        "/api/v1/recipes/install-plans/preview",
        headers=headers(),
        json={"recipe_revision_id": REVISION, "node_ids": [NODE]},
    )
    run = client.post(
        "/api/v1/recipes/run-plans/preview",
        headers=headers(),
        json={
            "installation_id": INSTALLATION,
            "placements": [{"node_id": NODE, "rank": 0, "role": "entrypoint"}],
        },
    )

    assert install.status_code == run.status_code == 200
    assert install.json()["nodes"][0]["free_after_bytes"] == 880
    assert run.json()["nodes"][0]["required_memory_bytes"] == 200
    assert len(install.json()["plan_digest"]) == 64


def test_execute_requires_exact_plan_hash_admin_and_idempotency_key() -> None:
    client, headers, recipes, audits = setup()
    denied = client.post(
        "/api/v1/recipes/installations",
        headers=headers("operator"),
        json={
            "recipe_revision_id": REVISION,
            "node_ids": [NODE],
            "plan_digest": "b" * 64,
            "request_key": "10000000-0000-4000-8000-000000000001",
        },
    )
    request_id = "20000000-0000-4000-8000-000000000001"
    accepted = client.post(
        "/api/v1/recipes/installations",
        headers={**headers(), "x-request-id": request_id},
        json={
            "recipe_revision_id": REVISION,
            "node_ids": [NODE],
            "plan_digest": "b" * 64,
            "request_key": "10000000-0000-4000-8000-000000000001",
        },
    )

    assert denied.status_code == 403
    assert accepted.status_code == 202
    assert accepted.json()["owner_id"] == INSTALLATION
    assert recipes.calls[-1][0] == "install"
    assert audits.for_request(request_id).action == "recipe.install"


def test_start_progress_stop_retry_and_uninstall_routes_are_stable() -> None:
    client, headers, _recipes, _audits = setup()
    start = client.post(
        "/api/v1/recipes/runs",
        headers=headers(),
        json={
            "installation_id": INSTALLATION,
            "alias": "qwen",
            "placements": [{"node_id": NODE, "rank": 0, "role": "entrypoint"}],
            "plan_digest": "c" * 64,
            "request_key": "10000000-0000-4000-8000-000000000002",
        },
    )
    progress = client.get(f"/api/v1/recipes/operations/{OPERATION}", headers=headers("viewer"))
    stop = client.post(
        f"/api/v1/recipes/runs/{RUN}/stop",
        headers=headers(),
        json={"request_key": "10000000-0000-4000-8000-000000000003"},
    )
    retry = client.post(
        f"/api/v1/recipes/operations/{OPERATION}/retry",
        headers=headers(),
        json={"request_key": "10000000-0000-4000-8000-000000000004"},
    )
    uninstall = client.post(
        f"/api/v1/recipes/installations/{INSTALLATION}/uninstall",
        headers=headers(),
        json={"request_key": "10000000-0000-4000-8000-000000000005"},
    )

    assert {start.status_code, stop.status_code, retry.status_code, uninstall.status_code} == {202}
    assert progress.status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/v1/recipes/install-plans/preview"]["post"]["operationId"] == "previewRecipeInstall"
    assert paths["/api/v1/recipes/runs"]["post"]["operationId"] == "startRecipeRun"
