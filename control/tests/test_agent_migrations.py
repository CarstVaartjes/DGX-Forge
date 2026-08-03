from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to(revision: str, database: str) -> None:
    command.upgrade(_config(database), revision)


def downgrade_to(revision: str, database: str) -> None:
    command.downgrade(_config(database), revision)


def tables(database: str) -> set[str]:
    return set(inspect(create_engine(database)).get_table_names())


def test_agent_migration_is_reversible(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"

    upgrade_to("0002_agent_operations", database)

    assert {
        "agent_nodes",
        "agent_certificates",
        "agent_operations",
        "agent_operation_attempts",
    } <= tables(database)

    downgrade_to("0001_operational_state", database)

    assert "agent_nodes" not in tables(database)
    assert "jobs" in tables(database)


def test_agent_models_capture_fenced_operation_state() -> None:
    from dgx_control.models import (
        AgentCertificate,
        AgentNode,
        AgentOperation,
        AgentOperationAttempt,
    )

    assert AgentNode.__table__.primary_key.columns.keys() == ["node_id"]
    assert AgentNode.__table__.c.capabilities.default is not None
    assert "private_key" not in AgentCertificate.__table__.c
    assert AgentOperation.__table__.c.parent_job_id.foreign_keys
    assert AgentOperation.__table__.c.node_id.foreign_keys
    assert AgentOperationAttempt.__table__.c.fence.unique
    assert any(
        constraint.columns.keys() == ["operation_id", "attempt"]
        for constraint in AgentOperationAttempt.__table__.constraints
    )
