from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
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
    agent_tables = {
        "agent_nodes",
        "agent_certificates",
        "agent_operations",
        "agent_operation_attempts",
    }

    upgrade_to("0001_operational_state", database)
    original_tables = tables(database)
    assert not (agent_tables & original_tables)

    upgrade_to("0002_agent_operations", database)
    assert agent_tables <= tables(database)

    downgrade_to("0001_operational_state", database)

    assert tables(database) == original_tables

    upgrade_to("0002_agent_operations", database)
    assert agent_tables <= tables(database)


def test_current_model_metadata_matches_head_schema(tmp_path: Path) -> None:
    from dgx_control.models import Base

    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    upgrade_to("head", database)

    assert set(Base.metadata.tables) == tables(database) - {"alembic_version"}
    engine = create_engine(database)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


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
    assert AgentOperation.__table__.c.retry_disposition.nullable
    assert AgentOperation.__table__.c.retry_disposition_attempt.nullable
    assert AgentOperationAttempt.__table__.c.fence.unique
    assert any(
        constraint.columns.keys() == ["operation_id", "attempt"]
        for constraint in AgentOperationAttempt.__table__.constraints
    )


def test_retry_disposition_migration_is_reversible(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    upgrade_to("0002_agent_operations", database)
    before = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_operations")}

    upgrade_to("0003_retry_disposition", database)
    after = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_operations")}
    assert after == before | {"retry_disposition", "retry_disposition_attempt"}

    downgrade_to("0002_agent_operations", database)
    downgraded = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_operations")}
    assert downgraded == before


def test_enrollment_migration_is_reversible_and_preserves_model_parity(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'control.sqlite'}"
    enrollment_tables = {"agent_enrollment_grants", "agent_enrollments"}

    upgrade_to("0003_retry_disposition", database)
    before = tables(database)
    assert not (enrollment_tables & before)

    upgrade_to("0004_agent_enrollment", database)
    assert enrollment_tables <= tables(database)
    grants = {column["name"] for column in inspect(create_engine(database)).get_columns("agent_enrollment_grants")}
    assert grants == {"id", "node_id", "token_digest", "created_by", "created_at", "expires_at", "consumed_at"}
    assert "token" not in grants

    downgrade_to("0003_retry_disposition", database)
    assert tables(database) == before

    upgrade_to("head", database)
    from dgx_control.models import Base

    engine = create_engine(database)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
