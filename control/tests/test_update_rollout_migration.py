from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_update_rollout_revision_follows_agent_runtime_identity() -> None:
    script = ScriptDirectory.from_config(_config("sqlite://"))

    revision = script.get_revision("0011_update_rollouts")

    assert revision is not None
    assert revision.down_revision == "0010_agent_runtime_identity"


def test_update_rollout_migration_is_reversible_and_complete(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'rollouts.sqlite'}"
    config = _config(database)
    engine = create_engine(database)
    command.upgrade(config, "0010_agent_runtime_identity")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_nodes (node_id, state, capabilities) "
                "VALUES ('spk_existing', 'active', '[]')"
            )
        )
    before = set(inspect(engine).get_table_names())

    command.upgrade(config, "0011_update_rollouts")
    database_inspector = inspect(engine)
    architecture = next(
        column
        for column in database_inspector.get_columns("agent_nodes")
        if column["name"] == "architecture"
    )
    assert architecture["nullable"] is True
    with engine.begin() as connection:
        assert connection.execute(
            text(
                "SELECT architecture FROM agent_nodes "
                "WHERE node_id = 'spk_existing'"
            )
        ).scalar_one() is None
    assert "ck_agent_nodes_architecture" in {
        constraint["name"]
        for constraint in database_inspector.get_check_constraints("agent_nodes")
    }
    assert set(database_inspector.get_table_names()) == before | {
        "node_mutation_leases",
        "update_authorization_intents",
        "update_rollout_nodes",
        "update_rollouts",
    }
    assert {
        column["name"]
        for column in database_inspector.get_columns("update_rollouts")
    } == {
        "id",
        "job_id",
        "state",
        "plan_digest",
        "release_digest",
        "base_commit",
        "fleet_digest",
        "topology_digest",
        "agent_input_digest",
        "target_platform_version",
        "target_build_digest",
        "tuf_targets_version",
        "update_admin_grant",
        "rollback_admin_grant",
        "plan",
        "current_batch",
        "soak_until",
        "failure_reason",
        "failure_evidence_digest",
        "rollback_evidence_digest",
        "approval_actor",
        "approval_request_id",
        "approval_reason",
        "approval_at",
        "approval_evidence_digest",
        "created_at",
        "updated_at",
        "completed_at",
    }
    assert {
        column["name"]
        for column in database_inspector.get_columns("update_authorization_intents")
    } == {
        "id",
        "rollout_id",
        "rollout_node_id",
        "parent_job_id",
        "node_id",
        "operation_id",
        "fence",
        "action",
        "state",
        "unsigned_payload",
        "payload_digest",
        "source_slot",
        "source_sha256",
        "source_generation",
        "target_release_digest",
        "expected_tuf_target_sha256",
        "expected_tuf_targets_version",
        "admin_grant",
        "admin_grant_digest",
        "expires_at",
        "request",
        "request_digest",
        "signed_response",
        "response_digest",
        "created_at",
        "updated_at",
        "queued_at",
    }
    assert {
        column["name"]
        for column in database_inspector.get_columns("update_rollout_nodes")
    } == {
        "id",
        "rollout_id",
        "node_id",
        "batch_index",
        "node_order",
        "is_canary",
        "state",
        "operation_id",
        "rollback_operation_id",
        "operation_history",
        "source_identity_digest",
        "target_artifact_digest",
        "observed_platform_version",
        "observed_build_digest",
        "observed_protocol_version",
        "observed_active_slot",
        "route_withdrawal_evidence_digest",
        "acceptance_evidence_digest",
        "failure_reason",
        "failure_evidence_digest",
        "rollback_evidence_digest",
        "soak_until",
        "dispatch_at",
        "activation_deadline",
        "created_at",
        "updated_at",
        "completed_at",
    }
    assert {
        column["name"]
        for column in database_inspector.get_columns("node_mutation_leases")
    } == {
        "node_id",
        "owner_kind",
        "owner_id",
        "fence",
        "state",
        "acquired_at",
        "updated_at",
    }

    command.downgrade(config, "0010_agent_runtime_identity")
    assert set(inspect(engine).get_table_names()) == before
    assert "architecture" not in {
        column["name"] for column in inspect(engine).get_columns("agent_nodes")
    }


def test_models_persist_pinned_rollout_and_node_evidence(tmp_path: Path) -> None:
    from vonk_control.models import (
        AgentNode,
        UpdateRollout,
        UpdateRolloutNode,
    )

    database = f"sqlite:///{tmp_path / 'model.sqlite'}"
    command.upgrade(_config(database), "head")
    engine = create_engine(database)
    Session = sessionmaker(engine)
    now = datetime(2026, 8, 5, tzinfo=UTC)
    digest = "a" * 64
    with Session.begin() as session:
        session.add(AgentNode(node_id="node-1", state="active", capabilities=[]))
        session.add(
            UpdateRollout(
                id="rollout-1",
                state="soaking",
                plan_digest=digest,
                release_digest="b" * 64,
                base_commit="c" * 40,
                fleet_digest="d" * 64,
                topology_digest="e" * 64,
                agent_input_digest="f" * 64,
                target_platform_version="1.1.0",
                target_build_digest=f"sha256:{'1' * 64}",
                tuf_targets_version=7,
                plan={"batches": [["node-1"]], "schema_version": 1},
                current_batch=0,
                soak_until=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UpdateRolloutNode(
                id="rollout-node-1",
                rollout_id="rollout-1",
                node_id="node-1",
                batch_index=0,
                node_order=0,
                is_canary=True,
                state="soaking",
                operation_history=[],
                source_identity_digest="2" * 64,
                target_artifact_digest="3" * 64,
                observed_platform_version="1.1.0",
                observed_build_digest=f"sha256:{'1' * 64}",
                observed_protocol_version=1,
                observed_active_slot="B",
                dispatch_at=now,
                activation_deadline=now + timedelta(minutes=2),
                route_withdrawal_evidence_digest="4" * 64,
                acceptance_evidence_digest="5" * 64,
                soak_until=now + timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )

    with Session() as session:
        rollout = session.get(UpdateRollout, "rollout-1")
        node = session.get(UpdateRolloutNode, "rollout-node-1")
        assert rollout is not None
        assert rollout.plan == {"batches": [["node-1"]], "schema_version": 1}
        assert rollout.plan_digest == digest
    assert node is not None
    assert node.is_canary is True
    assert node.observed_active_slot == "B"
    assert node.operation_history == []


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("state", "invented"),
        ("plan_digest", "short"),
        ("plan_digest", "A" * 64),
        ("plan_digest", "g" * 64),
        ("current_batch", "-1"),
    ],
)
def test_rollout_rejects_open_ended_state_and_invalid_pins(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database = f"sqlite:///{tmp_path / f'{column}.sqlite'}"
    command.upgrade(_config(database), "head")
    engine = create_engine(database)
    values = {
        "state": "planned",
        "plan_digest": "a" * 64,
        "current_batch": "0",
    }
    values[column] = value

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO update_rollouts "
                "(id,state,plan_digest,release_digest,base_commit,fleet_digest,"
                "topology_digest,agent_input_digest,target_platform_version,"
                "target_build_digest,plan,current_batch,created_at,updated_at) "
                "VALUES ('bad-rollout',:state,:plan_digest,:release_digest,"
                ":base_commit,:fleet_digest,:topology_digest,:agent_input_digest,"
                "'1.1.0',:target_build_digest,'{}',:current_batch,:now,:now)"
            ),
            {
                **values,
                "release_digest": "b" * 64,
                "base_commit": "c" * 40,
                "fleet_digest": "d" * 64,
                "topology_digest": "e" * 64,
                "agent_input_digest": "f" * 64,
                "target_build_digest": f"sha256:{'1' * 64}",
                "now": "2026-08-05 00:00:00+00:00",
            },
        )


def test_node_state_order_and_identity_are_bounded(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'node-constraints.sqlite'}"
    command.upgrade(_config(database), "head")
    engine = create_engine(database)
    now = "2026-08-05 00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_nodes (node_id,state,capabilities) "
                "VALUES ('node-1','active','[]')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO update_rollouts "
                "(id,state,plan_digest,release_digest,base_commit,fleet_digest,"
                "topology_digest,agent_input_digest,target_platform_version,"
                "target_build_digest,plan,current_batch,created_at,updated_at) "
                "VALUES ('rollout-1','planned',:a,:b,:commit,:d,:e,:f,'1.1.0',"
                ":build,'{}',0,:now,:now)"
            ),
            {
                "a": "a" * 64,
                "b": "b" * 64,
                "commit": "c" * 40,
                "d": "d" * 64,
                "e": "e" * 64,
                "f": "f" * 64,
                "build": f"sha256:{'1' * 64}",
                "now": now,
            },
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO update_rollout_nodes "
                "(id,rollout_id,node_id,batch_index,node_order,is_canary,state,"
                "source_identity_digest,target_artifact_digest,"
                "observed_active_slot,created_at,updated_at) VALUES "
                "('bad-node','rollout-1','node-1',-1,0,0,'unknown',:source,"
                ":target,'C',:now,:now)"
            ),
            {
                "source": "2" * 64,
                "target": "3" * 64,
                "now": now,
            },
        )


@pytest.mark.parametrize("state", ("failure-publishing", "rollback-publishing"))
def test_rollout_accepts_durable_route_restoration_states(
    tmp_path: Path, state: str
) -> None:
    database = f"sqlite:///{tmp_path / f'{state}.sqlite'}"
    command.upgrade(_config(database), "head")
    engine = create_engine(database)
    now = "2026-08-05 00:00:00+00:00"

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO update_rollouts "
                "(id,state,plan_digest,release_digest,base_commit,fleet_digest,"
                "topology_digest,agent_input_digest,target_platform_version,"
                "target_build_digest,plan,current_batch,created_at,updated_at) "
                "VALUES ('rollout-1',:state,:a,:b,:commit,:d,:e,:f,'1.1.0',"
                ":build,'{}',0,:now,:now)"
            ),
            {
                "state": state,
                "a": "a" * 64,
                "b": "b" * 64,
                "commit": "c" * 40,
                "d": "d" * 64,
                "e": "e" * 64,
                "f": "f" * 64,
                "build": f"sha256:{'1' * 64}",
                "now": now,
            },
        )


@pytest.mark.parametrize(
    ("state", "dispatch_at", "activation_deadline"),
    [
        ("updating", None, None),
        ("pending", "2026-08-05 00:00:00+00:00", "2026-08-05 00:01:00+00:00"),
        ("updating", "2026-08-05 00:01:00+00:00", "2026-08-05 00:00:00+00:00"),
    ],
)
def test_node_dispatch_window_is_complete_ordered_and_state_bound(
    tmp_path: Path,
    state: str,
    dispatch_at: str | None,
    activation_deadline: str | None,
) -> None:
    database = f"sqlite:///{tmp_path / f'dispatch-{state}-{dispatch_at}.sqlite'}"
    command.upgrade(_config(database), "head")
    engine = create_engine(database)
    now = "2026-08-05 00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO agent_nodes (node_id,state,capabilities) "
                "VALUES ('node-1','active','[]')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO update_rollouts "
                "(id,state,plan_digest,release_digest,base_commit,fleet_digest,"
                "topology_digest,agent_input_digest,target_platform_version,"
                "target_build_digest,plan,current_batch,created_at,updated_at) "
                "VALUES ('rollout-1','planned',:a,:b,:commit,:d,:e,:f,'1.1.0',"
                ":build,'{}',0,:now,:now)"
            ),
            {
                "a": "a" * 64,
                "b": "b" * 64,
                "commit": "c" * 40,
                "d": "d" * 64,
                "e": "e" * 64,
                "f": "f" * 64,
                "build": f"sha256:{'1' * 64}",
                "now": now,
            },
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO update_rollout_nodes "
                    "(id,rollout_id,node_id,batch_index,node_order,is_canary,state,"
                    "source_identity_digest,target_artifact_digest,dispatch_at,"
                    "activation_deadline,created_at,updated_at) VALUES "
                    "('bad-node','rollout-1','node-1',0,0,0,:state,:source,"
                    ":target,:dispatch_at,:activation_deadline,:now,:now)"
                ),
                {
                    "state": state,
                    "source": "2" * 64,
                    "target": "3" * 64,
                    "dispatch_at": dispatch_at,
                    "activation_deadline": activation_deadline,
                    "now": now,
                },
            )
