"""Persist topology-aware GPU node agent update rollouts."""

import sqlalchemy as sa
from alembic import op

revision = "0011_update_rollouts"
down_revision = "0010_agent_runtime_identity"
branch_labels = None
depends_on = None


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) AND "
        f"length({remainder}) = 0"
    )


def _nullable_lower_hex(column: str, length: int) -> str:
    return f"{column} IS NULL OR ({_lower_hex(column, length)})"


def _uuid_shape(column: str) -> str:
    compact = f"replace({column}, '-', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' AND "
        f"substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' AND "
        f"substr({column}, 24, 1) = '-' AND ({_lower_hex(compact, 32)})"
    )


def upgrade() -> None:
    with op.batch_alter_table("agent_nodes") as batch:
        batch.add_column(sa.Column("architecture", sa.String(length=16)))
        batch.add_column(sa.Column("supervisor_ready_generation", sa.Integer()))
        batch.add_column(
            sa.Column(
                "self_test_passed",
                sa.Boolean(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("contact_certificate_serial", sa.String(length=128)))
        batch.add_column(sa.Column("contact_observation_digest", sa.String(length=64)))
        batch.create_check_constraint(
            "ck_agent_nodes_architecture",
            "architecture IS NULL OR architecture IN ('linux-arm64', 'linux-x86_64')",
        )

    op.create_table(
        "node_mutation_leases",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("owner_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("fence", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "owner_kind IN ('update-rollout', 'reconciliation')",
            name="ck_node_mutation_leases_owner_kind",
        ),
        sa.CheckConstraint(
            "state IN ('held', 'releasing')",
            name="ck_node_mutation_leases_state",
        ),
        sa.CheckConstraint(
            _uuid_shape("owner_id"),
            name="ck_node_mutation_leases_owner_id_shape",
        ),
        sa.CheckConstraint(
            _uuid_shape("fence"),
            name="ck_node_mutation_leases_fence_shape",
        ),
        sa.CheckConstraint(
            "updated_at >= acquired_at",
            name="ck_node_mutation_leases_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["agent_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index(
        "ix_node_mutation_leases_owner",
        "node_mutation_leases",
        ["owner_kind", "owner_id"],
        unique=False,
    )

    op.create_table(
        "update_rollouts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36)),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=False),
        sa.Column("base_commit", sa.String(length=128), nullable=False),
        sa.Column("fleet_digest", sa.String(length=64), nullable=False),
        sa.Column("topology_digest", sa.String(length=64), nullable=False),
        sa.Column("agent_input_digest", sa.String(length=64), nullable=False),
        sa.Column("target_platform_version", sa.String(length=32), nullable=False),
        sa.Column("target_build_digest", sa.String(length=71), nullable=False),
        sa.Column(
            "tuf_targets_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("update_admin_grant", sa.JSON()),
        sa.Column("rollback_admin_grant", sa.JSON()),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("current_batch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("soak_until", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("failure_evidence_digest", sa.String(length=64)),
        sa.Column("rollback_evidence_digest", sa.String(length=64)),
        sa.Column("approval_actor", sa.String(length=200)),
        sa.Column("approval_request_id", sa.String(length=36)),
        sa.Column("approval_reason", sa.Text()),
        sa.Column("approval_at", sa.DateTime(timezone=True)),
        sa.Column("approval_evidence_digest", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('planned', 'withdrawing', 'updating', 'soaking', "
            "'publishing', 'failure-publishing', 'compensating-withdrawal', "
            "'paused', 'rolling-back', "
            "'rollback-publishing', 'waiting-for-approval', 'completed', 'partial', 'failed')",
            name="ck_update_rollouts_state",
        ),
        sa.CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_update_rollouts_plan_digest_length",
        ),
        sa.CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_update_rollouts_release_digest_length",
        ),
        sa.CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_update_rollouts_fleet_digest_length",
        ),
        sa.CheckConstraint(
            _lower_hex("topology_digest", 64),
            name="ck_update_rollouts_topology_digest_length",
        ),
        sa.CheckConstraint(
            _lower_hex("agent_input_digest", 64),
            name="ck_update_rollouts_agent_input_digest_length",
        ),
        sa.CheckConstraint(
            "length(target_build_digest) = 71 AND "
            "substr(target_build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(target_build_digest, 8, 64)', 64)})",
            name="ck_update_rollouts_target_build_digest",
        ),
        sa.CheckConstraint(
            "current_batch >= 0",
            name="ck_update_rollouts_current_batch",
        ),
        sa.CheckConstraint(
            "tuf_targets_version >= 1",
            name="ck_update_rollouts_tuf_targets_version",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_update_rollouts_failure_evidence_digest_length",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_update_rollouts_rollback_evidence_digest_length",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("approval_evidence_digest", 64),
            name="ck_update_rollouts_approval_evidence_digest_length",
        ),
        sa.CheckConstraint(
            "(approval_at IS NULL AND approval_actor IS NULL AND "
            "approval_request_id IS NULL AND approval_reason IS NULL AND "
            "approval_evidence_digest IS NULL) OR "
            "(approval_at IS NOT NULL AND approval_actor IS NOT NULL AND "
            "approval_request_id IS NOT NULL AND approval_reason IS NOT NULL AND "
            "approval_evidence_digest IS NOT NULL)",
            name="ck_update_rollouts_approval_complete",
        ),
        sa.CheckConstraint(
            "(state IN ('completed', 'partial') AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed', 'partial') AND completed_at IS NULL)",
            name="ck_update_rollouts_completion_state",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_request_id"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("plan_digest"),
    )
    op.create_index(
        "ix_update_rollouts_created_at",
        "update_rollouts",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_update_rollouts_state",
        "update_rollouts",
        ["state"],
        unique=False,
    )

    op.create_table(
        "update_rollout_nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rollout_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("node_order", sa.Integer(), nullable=False),
        sa.Column("is_canary", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("operation_id", sa.String(length=36)),
        sa.Column("rollback_operation_id", sa.String(length=36)),
        sa.Column(
            "operation_history",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("source_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("target_artifact_digest", sa.String(length=64), nullable=False),
        sa.Column("observed_platform_version", sa.String(length=32)),
        sa.Column("observed_build_digest", sa.String(length=71)),
        sa.Column("observed_protocol_version", sa.Integer()),
        sa.Column("observed_active_slot", sa.String(length=1)),
        sa.Column("route_withdrawal_evidence_digest", sa.String(length=64)),
        sa.Column("acceptance_evidence_digest", sa.String(length=64)),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("failure_evidence_digest", sa.String(length=64)),
        sa.Column("rollback_evidence_digest", sa.String(length=64)),
        sa.Column("soak_until", sa.DateTime(timezone=True)),
        sa.Column("dispatch_at", sa.DateTime(timezone=True)),
        sa.Column("activation_deadline", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('offline-pending', 'pending', 'routes-withdrawn', 'updating', 'soaking', "
            "'accepted', 'failed', 'rolling-back', 'rolled-back')",
            name="ck_update_rollout_nodes_state",
        ),
        sa.CheckConstraint(
            "batch_index >= -1 AND node_order >= 0",
            name="ck_update_rollout_nodes_order",
        ),
        sa.CheckConstraint(
            _lower_hex("source_identity_digest", 64),
            name="ck_update_rollout_nodes_source_identity_digest_length",
        ),
        sa.CheckConstraint(
            _lower_hex("target_artifact_digest", 64),
            name="ck_update_rollout_nodes_target_artifact_digest_length",
        ),
        sa.CheckConstraint(
            "observed_build_digest IS NULL OR "
            "(length(observed_build_digest) = 71 AND "
            "substr(observed_build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(observed_build_digest, 8, 64)', 64)}))",
            name="ck_update_rollout_nodes_observed_build_digest",
        ),
        sa.CheckConstraint(
            "observed_active_slot IS NULL OR observed_active_slot IN ('A', 'B')",
            name="ck_update_rollout_nodes_observed_active_slot",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("route_withdrawal_evidence_digest", 64),
            name="ck_update_rollout_nodes_route_evidence_digest_length",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("acceptance_evidence_digest", 64),
            name="ck_update_rollout_nodes_acceptance_evidence_digest_length",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_update_rollout_nodes_failure_evidence_digest_length",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_update_rollout_nodes_rollback_evidence_digest_length",
        ),
        sa.CheckConstraint(
            "(state IN ('offline-pending', 'pending', 'routes-withdrawn') AND dispatch_at IS NULL "
            "AND activation_deadline IS NULL) OR "
            "(state IN ('updating', 'soaking', 'accepted', 'failed', "
            "'rolling-back', 'rolled-back') AND dispatch_at IS NOT NULL AND "
            "activation_deadline IS NOT NULL AND activation_deadline > dispatch_at)",
            name="ck_update_rollout_nodes_dispatch_window",
        ),
        sa.ForeignKeyConstraint(["node_id"], ["agent_nodes.node_id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["agent_operations.id"]),
        sa.ForeignKeyConstraint(
            ["rollback_operation_id"], ["agent_operations.id"]
        ),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["update_rollouts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
        sa.UniqueConstraint("rollback_operation_id"),
        sa.UniqueConstraint(
            "rollout_id",
            "batch_index",
            "node_order",
            name="uq_update_rollout_nodes_batch_order",
        ),
        sa.UniqueConstraint(
            "rollout_id",
            "node_id",
            name="uq_update_rollout_nodes_rollout_node",
        ),
    )
    op.create_index(
        "ix_update_rollout_nodes_node_id",
        "update_rollout_nodes",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        "ix_update_rollout_nodes_rollout_id",
        "update_rollout_nodes",
        ["rollout_id"],
        unique=False,
    )
    op.create_index(
        "ix_update_rollout_nodes_state",
        "update_rollout_nodes",
        ["state"],
        unique=False,
    )
    op.create_index(
        "ix_update_rollout_nodes_created_at",
        "update_rollout_nodes",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "update_authorization_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rollout_id", sa.String(length=36), nullable=False),
        sa.Column("rollout_node_id", sa.String(length=36), nullable=False),
        sa.Column("parent_job_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("fence", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("unsigned_payload", sa.JSON(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("source_slot", sa.String(length=1), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("target_release_digest", sa.String(length=71)),
        sa.Column("expected_tuf_target_sha256", sa.String(length=64)),
        sa.Column("expected_tuf_targets_version", sa.Integer()),
        sa.Column("admin_grant", sa.JSON(), nullable=False),
        sa.Column("admin_grant_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("signed_response", sa.JSON()),
        sa.Column("response_digest", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "action IN ('agent.update', 'agent.rollback')",
            name="ck_update_authorization_intents_action",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'signed', 'queued', 'stale')",
            name="ck_update_authorization_intents_state",
        ),
        sa.CheckConstraint(
            "source_slot IN ('A', 'B')",
            name="ck_update_authorization_intents_source_slot",
        ),
        sa.CheckConstraint(
            _lower_hex("payload_digest", 64),
            name="ck_update_authorization_intents_payload_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("source_sha256", 64),
            name="ck_update_authorization_intents_source_sha256",
        ),
        sa.CheckConstraint(
            _lower_hex("request_digest", 64),
            name="ck_update_authorization_intents_request_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("response_digest", 64),
            name="ck_update_authorization_intents_response_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("admin_grant_digest", 64),
            name="ck_update_authorization_intents_admin_grant_digest",
        ),
        sa.CheckConstraint(
            "target_release_digest IS NULL OR "
            "(length(target_release_digest) = 71 AND "
            "substr(target_release_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(target_release_digest, 8, 64)', 64)}))",
            name="ck_update_authorization_intents_target_release_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("expected_tuf_target_sha256", 64),
            name="ck_update_authorization_intents_tuf_target_sha256",
        ),
        sa.CheckConstraint(
            "(action = 'agent.update' AND target_release_digest IS NOT NULL AND "
            "expected_tuf_target_sha256 IS NOT NULL AND "
            "expected_tuf_targets_version IS NOT NULL AND "
            "expected_tuf_targets_version >= 1) OR "
            "(action = 'agent.rollback' AND target_release_digest IS NULL AND "
            "expected_tuf_target_sha256 IS NULL AND "
            "expected_tuf_targets_version IS NULL)",
            name="ck_update_authorization_intents_tuf_binding",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND signed_response IS NULL AND response_digest IS NULL "
            "AND queued_at IS NULL) OR "
            "(state = 'signed' AND signed_response IS NOT NULL AND response_digest IS NOT NULL "
            "AND queued_at IS NULL) OR "
            "(state = 'queued' AND signed_response IS NOT NULL AND response_digest IS NOT NULL "
            "AND queued_at IS NOT NULL) OR state = 'stale'",
            name="ck_update_authorization_intents_state_payload",
        ),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["update_rollouts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rollout_node_id"], ["update_rollout_nodes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_job_id"], ["jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["agent_nodes.node_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
        sa.UniqueConstraint("fence"),
    )
    for columns, name in (
        (("rollout_id",), "ix_update_authorization_intents_rollout_id"),
        (("rollout_node_id",), "ix_update_authorization_intents_rollout_node_id"),
        (("parent_job_id",), "ix_update_authorization_intents_parent_job_id"),
        (("node_id",), "ix_update_authorization_intents_node_id"),
        (("state",), "ix_update_authorization_intents_state"),
    ):
        op.create_index(name, "update_authorization_intents", list(columns))


def downgrade() -> None:
    op.drop_table("update_authorization_intents")
    op.drop_index(
        "ix_update_rollout_nodes_created_at", table_name="update_rollout_nodes"
    )
    op.drop_index("ix_update_rollout_nodes_state", table_name="update_rollout_nodes")
    op.drop_index(
        "ix_update_rollout_nodes_rollout_id", table_name="update_rollout_nodes"
    )
    op.drop_index("ix_update_rollout_nodes_node_id", table_name="update_rollout_nodes")
    op.drop_table("update_rollout_nodes")
    op.drop_index("ix_update_rollouts_state", table_name="update_rollouts")
    op.drop_index("ix_update_rollouts_created_at", table_name="update_rollouts")
    op.drop_table("update_rollouts")
    op.drop_index(
        "ix_node_mutation_leases_owner", table_name="node_mutation_leases"
    )
    op.drop_table("node_mutation_leases")
    with op.batch_alter_table("agent_nodes") as batch:
        batch.drop_column("contact_observation_digest")
        batch.drop_column("contact_certificate_serial")
        batch.drop_column("self_test_passed")
        batch.drop_column("supervisor_ready_generation")
        batch.drop_constraint("ck_agent_nodes_architecture", type_="check")
        batch.drop_column("architecture")
