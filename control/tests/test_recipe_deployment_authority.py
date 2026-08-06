from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from dgx_control.catalog_service import CatalogService, RecipeDraftInput
from dgx_control.models import Base
from dgx_control.recipe_deployments import RecipeDeploymentService


def migration_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    value = Config(root / "alembic.ini")
    value.set_main_option("script_location", str(root / "migrations"))
    value.set_main_option("sqlalchemy.url", database_url)
    return value


def test_recipe_deployment_authority_is_linear_head() -> None:
    script = ScriptDirectory.from_config(migration_config("sqlite://"))
    assert script.get_heads() == ["0016_recipe_deployment_authority"]
    assert script.get_revision("0016_recipe_deployment_authority").down_revision == (
        "0015_recipe_catalog"
    )


def test_migration_backfills_legacy_authority_and_allows_recipe_authority(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'authority.sqlite'}"
    config = migration_config(url)
    command.upgrade(config, "0015_recipe_catalog")
    engine = create_engine(url)
    now = "2026-08-07 12:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO package_rollouts "
                "(id,deployment_id,deployment_digest,release_digest,base_commit,"
                "policy_digest,tuf_target_digest,fleet_digest,topology_digest,plan_digest,"
                "state,actor,current_batch,created_at,updated_at) VALUES "
                "('10000000-0000-4000-8000-000000000001','demo',:digest,:digest,:commit,"
                ":digest,:digest,:digest,:digest,:plan,'planned','admin',0,:now,:now)"
            ),
            {
                "digest": "a" * 64,
                "plan": "b" * 64,
                "commit": "c" * 40,
                "now": now,
            },
        )
    command.upgrade(config, "head")
    columns = {column["name"]: column for column in inspect(engine).get_columns("package_rollouts")}
    assert columns["base_commit"]["nullable"] is True
    assert columns["recipe_revision_id"]["nullable"] is True
    assert columns["authority_digest"]["nullable"] is False
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT authority_digest,recipe_revision_id FROM package_rollouts")
        ).one()
    assert len(row.authority_digest) == 64
    assert row.recipe_revision_id is None


def test_resolved_recipe_plans_without_git_remote(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    catalog = CatalogService(sessions, clock=clock)
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    draft = catalog.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=document)
    )
    resolved = catalog.resolve(draft.recipe_id, draft.revision_number, "admin")
    git = _ForbiddenGit()
    service = RecipeDeploymentService(sessions, clock=clock, repository=git)

    plan = service.preview_recipe(
        resolved.id,
        alias="qwen3",
        placements=[{"node_id": "spk_" + "1" * 32, "rank": 0, "role": "entrypoint"}],
        actor="admin",
    )

    assert plan.recipe_revision_id == resolved.id
    assert len(plan.placement_digest) == len(plan.plan_digest) == 64
    assert plan.base_commit is None
    assert git.calls == []

    payloads = service.agent_payloads(plan, operation_fence="f" * 64)
    assert payloads == (
        {
            "schema_version": 1,
            "operation_fence": "f" * 64,
            "recipe_revision_id": resolved.id,
            "recipe_content_sha256": resolved.content_sha256,
            "plan_digest": plan.plan_digest,
            "placement_digest": plan.placement_digest,
            "node_id": "spk_" + "1" * 32,
            "rank": 0,
            "role": "entrypoint",
            "runtime": plan.payload["runtime"],
            "artifacts": plan.payload["artifacts"],
            "endpoint": plan.payload["endpoint"],
            "security": plan.payload["security"],
        },
    )


class _ForbiddenGit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def head(self) -> str:
        self.calls.append("head")
        raise AssertionError("recipe deployment consulted Git")
