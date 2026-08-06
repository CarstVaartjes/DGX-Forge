from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dgx_control.api import create_app
from dgx_control.audit import MemoryAuditStore
from dgx_control.auth import Actor, TokenCodec
from dgx_control.catalog_service import CatalogService
from dgx_control.models import Base


class Jobs:
    def get(self, job_id: str):
        raise KeyError(job_id)

    def list(self, *, limit: int = 100):
        return []

    def list_page(self, **_kwargs):
        return [], None, 0


@pytest.fixture
def recipe_document() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def api(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions, clock=lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    )
    codec = TokenCodec(b"c" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=audits, fleet=lambda: {"nodes": []},
        now=lambda: 10, catalog=service,
    )

    def headers(role: str) -> dict[str, str]:
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, audits


def test_operator_cannot_author_recipe(api, recipe_document) -> None:
    client, headers, _audits = api
    response = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("operator"),
        json={"slug": "qwen3-vllm", "document": recipe_document},
    )

    assert response.status_code == 403


def test_create_list_get_and_resolve_recipe(api, recipe_document) -> None:
    client, headers, audits = api
    request_id = "20000000-0000-4000-8000-000000000002"
    created = client.post(
        "/api/v1/catalog/recipes",
        headers={**headers("administrator"), "x-request-id": request_id},
        json={"slug": "qwen3-vllm", "document": recipe_document},
    )
    assert created.status_code == 201
    recipe_id = created.json()["recipe_id"]

    listed = client.get("/api/v1/catalog/recipes", headers=headers("viewer"))
    detail = client.get(
        f"/api/v1/catalog/recipes/{recipe_id}", headers=headers("viewer")
    )
    resolved = client.post(
        f"/api/v1/catalog/recipes/{recipe_id}/resolve",
        headers=headers("administrator"),
        json={"expected_revision": 1},
    )

    assert listed.status_code == detail.status_code == resolved.status_code == 200
    assert listed.json()["recipes"][0]["origin"] == "local"
    assert detail.json()["document"] == recipe_document
    assert resolved.json()["lifecycle"] == "resolved"
    assert len(resolved.json()["content_sha256"]) == 64
    audit = audits.for_request(request_id)
    assert audit.action == "catalog.recipe.create"
    assert audit.targets == (recipe_id,)


def test_stale_draft_returns_stable_problem(api, recipe_document) -> None:
    client, headers, _audits = api
    created = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={"slug": "qwen3-vllm", "document": recipe_document},
    ).json()
    recipe_document["metadata"]["title"] = "Updated"
    first = client.put(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/draft",
        headers=headers("administrator"),
        json={"expected_revision": 1, "document": recipe_document},
    )
    stale = client.put(
        f"/api/v1/catalog/recipes/{created['recipe_id']}/draft",
        headers=headers("administrator"),
        json={"expected_revision": 1, "document": recipe_document},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["code"] == "catalog.stale_revision"
    assert len(stale.json()["request_id"]) == 36


def test_recipe_body_is_bounded_and_unknown_fields_are_rejected(
    api, recipe_document
) -> None:
    client, headers, _audits = api
    response = client.post(
        "/api/v1/catalog/recipes",
        headers=headers("administrator"),
        json={
            "slug": "qwen3-vllm",
            "document": recipe_document,
            "authorization": "Bearer never-reflect-me",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "catalog.invalid_request"
    assert "authorization" not in response.text.lower()
    assert "never-reflect-me" not in response.text


def test_catalog_operation_ids_are_stable(api) -> None:
    client, _headers, _audits = api
    paths = client.get("/openapi.json").json()["paths"]

    assert paths["/api/v1/catalog/recipes"]["get"]["operationId"] == "listLocalRecipes"
    assert paths["/api/v1/catalog/recipes"]["post"]["operationId"] == "createLocalRecipe"
    assert paths["/api/v1/catalog/recipes/{recipe_id}"]["get"]["operationId"] == "getLocalRecipe"
    assert paths["/api/v1/catalog/recipes/{recipe_id}/draft"]["put"]["operationId"] == "updateLocalRecipeDraft"
    assert paths["/api/v1/catalog/recipes/{recipe_id}/resolve"]["post"]["operationId"] == "resolveLocalRecipe"
    assert paths["/api/v1/catalog/recipes/{recipe_id}/fork"]["post"]["operationId"] == "forkLocalRecipe"
