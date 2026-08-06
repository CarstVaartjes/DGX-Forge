"""Persist generic workload package operational state.

Git and workload TUF remain the authority for package definitions and release
locks.  This migration stores only retry-safe operational projections and
digest bindings; it intentionally has no column for lock bytes or payloads.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_workload_packages"
down_revision = "0012_control_process_heartbeats"
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


def upgrade() -> None:
    op.create_table(
        "package_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("family_id", sa.String(length=128), nullable=False),
        sa.Column("upstream_identity_digest", sa.String(length=64), nullable=False),
        sa.Column("metadata_digest", sa.String(length=64), nullable=False),
        sa.Column("upstream_version", sa.String(length=256), nullable=False),
        sa.Column("channel", sa.String(length=128)),
        sa.Column("source_provider", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.String(length=1024), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=80)),
        sa.Column("reason_detail", sa.JSON()),
        sa.Column("summary", sa.JSON()),
        sa.Column("discovered_by", sa.String(length=200), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(family_id) BETWEEN 1 AND 128",
            name="ck_package_candidates_family_id_length",
        ),
        sa.CheckConstraint(
            _lower_hex("upstream_identity_digest", 64),
            name="ck_package_candidates_upstream_identity_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("metadata_digest", 64),
            name="ck_package_candidates_metadata_digest",
        ),
        sa.CheckConstraint(
            "state IN ('discovered', 'resolving', 'resolved', 'unsupported', "
            "'quarantined', 'rejected')",
            name="ck_package_candidates_state",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_candidates_reason_code_length",
        ),
        sa.CheckConstraint(
            "reason_detail IS NULL OR length(CAST(reason_detail AS TEXT)) <= 8192",
            name="ck_package_candidates_reason_detail_size",
        ),
        sa.CheckConstraint(
            "length(source_provider) BETWEEN 1 AND 64",
            name="ck_package_candidates_source_provider_length",
        ),
        sa.CheckConstraint(
            "length(source_reference) BETWEEN 1 AND 1024",
            name="ck_package_candidates_source_reference_length",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "family_id",
            "upstream_identity_digest",
            "metadata_digest",
            name="uq_package_candidates_identity",
        ),
    )
    for name, columns in (
        ("ix_package_candidates_family_id", ["family_id"]),
        ("ix_package_candidates_state", ["state"]),
        ("ix_package_candidates_first_seen_at", ["first_seen_at"]),
        ("ix_package_candidates_last_seen_at", ["last_seen_at"]),
        ("ix_package_candidates_created_at", ["created_at"]),
    ):
        op.create_index(name, "package_candidates", columns, unique=False)

    op.create_table(
        "package_resolutions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("resolver_id", sa.String(length=128), nullable=False),
        sa.Column("resolver_schema_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("release_digest", sa.String(length=64)),
        sa.Column("reason_code", sa.String(length=80)),
        sa.Column("reason_detail", sa.JSON()),
        sa.Column("summary", sa.JSON()),
        sa.Column("resolved_by", sa.String(length=200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'resolving', 'resolved', 'unsupported', "
            "'incompatible', 'quarantined', 'rejected')",
            name="ck_package_resolutions_state",
        ),
        sa.CheckConstraint(
            "resolver_schema_version >= 1",
            name="ck_package_resolutions_schema_version",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("release_digest", 64),
            name="ck_package_resolutions_release_digest",
        ),
        sa.CheckConstraint(
            "(state = 'resolved' AND release_digest IS NOT NULL) OR "
            "(state <> 'resolved')",
            name="ck_package_resolutions_resolved_release_binding",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_resolutions_reason_code_length",
        ),
        sa.CheckConstraint(
            "reason_detail IS NULL OR length(CAST(reason_detail AS TEXT)) <= 8192",
            name="ck_package_resolutions_reason_detail_size",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["package_candidates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "resolver_id",
            "resolver_schema_version",
            name="uq_package_resolutions_candidate_resolver_schema",
        ),
    )
    for name, columns in (
        ("ix_package_resolutions_candidate_id", ["candidate_id"]),
        ("ix_package_resolutions_state", ["state"]),
        ("ix_package_resolutions_release_digest", ["release_digest"]),
        ("ix_package_resolutions_created_at", ["created_at"]),
    ):
        op.create_index(name, "package_resolutions", columns, unique=False)

    op.create_table(
        "package_validation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("resolution_id", sa.String(length=36), nullable=False),
        sa.Column("validation_kind", sa.String(length=24), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("fleet_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason_code", sa.String(length=80)),
        sa.Column("failure_detail", sa.JSON()),
        sa.Column("evidence", sa.JSON()),
        sa.Column("progress", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('planned', 'running', 'passed', 'failed', 'retryable', "
            "'rejected', 'cancelled')",
            name="ck_package_validation_runs_state",
        ),
        sa.CheckConstraint(
            "validation_kind IN ('artifact', 'health', 'inference', 'compatibility')",
            name="ck_package_validation_runs_kind",
        ),
        sa.CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_validation_runs_release_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("policy_digest", 64),
            name="ck_package_validation_runs_policy_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_package_validation_runs_fleet_digest",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_package_validation_runs_attempt"),
        sa.CheckConstraint(
            "reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80",
            name="ck_package_validation_runs_reason_code_length",
        ),
        sa.CheckConstraint(
            "failure_detail IS NULL OR length(CAST(failure_detail AS TEXT)) <= 8192",
            name="ck_package_validation_runs_failure_detail_size",
        ),
        sa.CheckConstraint(
            "evidence IS NULL OR length(CAST(evidence AS TEXT)) <= 16384",
            name="ck_package_validation_runs_evidence_size",
        ),
        sa.CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 8192",
            name="ck_package_validation_runs_progress_size",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["package_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["resolution_id"], ["package_resolutions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resolution_id",
            "validation_kind",
            "policy_digest",
            "fleet_digest",
            name="uq_package_validation_runs_binding",
        ),
    )
    for name, columns in (
        ("ix_package_validation_runs_candidate_id", ["candidate_id"]),
        ("ix_package_validation_runs_resolution_id", ["resolution_id"]),
        ("ix_package_validation_runs_release_digest", ["release_digest"]),
        ("ix_package_validation_runs_state", ["state"]),
        ("ix_package_validation_runs_created_at", ["created_at"]),
    ):
        op.create_index(name, "package_validation_runs", columns, unique=False)

    op.create_table(
        "package_rollouts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36)),
        sa.Column("deployment_id", sa.String(length=128), nullable=False),
        sa.Column("deployment_digest", sa.String(length=64), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=False),
        sa.Column("previous_release_digest", sa.String(length=64)),
        sa.Column("base_commit", sa.String(length=128), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("tuf_target_digest", sa.String(length=64), nullable=False),
        sa.Column("fleet_digest", sa.String(length=64), nullable=False),
        sa.Column("topology_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("plan", sa.JSON()),
        sa.Column("progress", sa.JSON()),
        sa.Column("current_batch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("failure_evidence_digest", sa.String(length=64)),
        sa.Column("rollback_evidence_digest", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('planned', 'preparing', 'activating', 'health-checking', "
            "'soaking', 'paused', 'rolling-back', 'completed', 'failed', "
            "'rolled-back', 'cancelled', 'running', 'partial', 'waiting-for-operator')",
            name="ck_package_rollouts_state",
        ),
        sa.CheckConstraint(
            _lower_hex("deployment_digest", 64),
            name="ck_package_rollouts_deployment_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_rollouts_release_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("previous_release_digest", 64),
            name="ck_package_rollouts_previous_release_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("policy_digest", 64),
            name="ck_package_rollouts_policy_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("tuf_target_digest", 64),
            name="ck_package_rollouts_tuf_target_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("fleet_digest", 64),
            name="ck_package_rollouts_fleet_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("topology_digest", 64),
            name="ck_package_rollouts_topology_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_package_rollouts_plan_digest",
        ),
        sa.CheckConstraint(
            "length(base_commit) BETWEEN 40 AND 128",
            name="ck_package_rollouts_base_commit_length",
        ),
        sa.CheckConstraint(
            "current_batch >= 0", name="ck_package_rollouts_current_batch"
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) <= 1024",
            name="ck_package_rollouts_failure_reason_size",
        ),
        sa.CheckConstraint(
            "plan IS NULL OR length(CAST(plan AS TEXT)) <= 32768",
            name="ck_package_rollouts_plan_size",
        ),
        sa.CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 16384",
            name="ck_package_rollouts_progress_size",
        ),
        sa.CheckConstraint(
            "failure_evidence_digest IS NULL OR "
            f"({_lower_hex('failure_evidence_digest', 64)})",
            name="ck_package_rollouts_failure_evidence_digest",
        ),
        sa.CheckConstraint(
            "rollback_evidence_digest IS NULL OR "
            f"({_lower_hex('rollback_evidence_digest', 64)})",
            name="ck_package_rollouts_rollback_evidence_digest",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("plan_digest"),
        sa.UniqueConstraint(
            "deployment_id",
            "release_digest",
            "base_commit",
            "plan_digest",
            name="uq_package_rollouts_deployment_release_commit_plan",
        ),
    )
    for name, columns in (
        ("ix_package_rollouts_deployment_id", ["deployment_id"]),
        ("ix_package_rollouts_release_digest", ["release_digest"]),
        ("ix_package_rollouts_state", ["state"]),
        ("ix_package_rollouts_created_at", ["created_at"]),
    ):
        op.create_index(name, "package_rollouts", columns, unique=False)

    op.create_table(
        "package_rollout_nodes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rollout_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("node_order", sa.Integer(), nullable=False),
        sa.Column("is_canary", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("operation_kind", sa.String(length=80)),
        sa.Column("graph_operation_id", sa.String(length=128)),
        sa.Column("operation_key", sa.String(length=128)),
        sa.Column("operation_id", sa.String(length=128)),
        sa.Column("rollback_operation_id", sa.String(length=128)),
        sa.Column(
            "operation_history",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("expected_payload_digest", sa.String(length=64), nullable=False),
        sa.Column("observed_release_digest", sa.String(length=64)),
        sa.Column("evidence_digest", sa.String(length=64)),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("failure_evidence_digest", sa.String(length=64)),
        sa.Column("rollback_evidence_digest", sa.String(length=64)),
        sa.Column("progress", sa.JSON()),
        sa.Column("dispatch_at", sa.DateTime(timezone=True)),
        sa.Column("activation_deadline", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('offline-pending', 'pending', 'queued', 'running', 'preparing', 'prepared', "
            "'activating', 'health-checking', 'accepted', 'failed', "
            "'rolling-back', 'rolled-back', 'cancelled')",
            name="ck_package_rollout_nodes_state",
        ),
        sa.CheckConstraint(
            "batch_index >= -1 AND node_order >= 0",
            name="ck_package_rollout_nodes_order",
        ),
        sa.CheckConstraint(
            _lower_hex("expected_payload_digest", 64),
            name="ck_package_rollout_nodes_expected_payload_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("observed_release_digest", 64),
            name="ck_package_rollout_nodes_observed_release_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("evidence_digest", 64),
            name="ck_package_rollout_nodes_evidence_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("failure_evidence_digest", 64),
            name="ck_package_rollout_nodes_failure_evidence_digest",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("rollback_evidence_digest", 64),
            name="ck_package_rollout_nodes_rollback_evidence_digest",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) <= 1024",
            name="ck_package_rollout_nodes_failure_reason_size",
        ),
        sa.CheckConstraint(
            "progress IS NULL OR length(CAST(progress AS TEXT)) <= 8192",
            name="ck_package_rollout_nodes_progress_size",
        ),
        sa.CheckConstraint(
            "length(CAST(operation_history AS TEXT)) <= 16384",
            name="ck_package_rollout_nodes_operation_history_size",
        ),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["package_rollouts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["agent_nodes.node_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
        sa.UniqueConstraint("rollback_operation_id"),
        sa.UniqueConstraint(
            "rollout_id", "node_id", name="uq_package_rollout_nodes_rollout_node"
        ),
        sa.UniqueConstraint(
            "rollout_id",
            "batch_index",
            "node_order",
            name="uq_package_rollout_nodes_batch_order",
        ),
    )
    for name, columns in (
        ("ix_package_rollout_nodes_rollout_id", ["rollout_id"]),
        ("ix_package_rollout_nodes_node_id", ["node_id"]),
        ("ix_package_rollout_nodes_state", ["state"]),
        ("ix_package_rollout_nodes_created_at", ["created_at"]),
    ):
        op.create_index(name, "package_rollout_nodes", columns, unique=False)

    op.create_table(
        "package_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("deployment_id", sa.String(length=128), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=False),
        sa.Column("observation_digest", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=128)),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("summary", sa.JSON()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "length(deployment_id) BETWEEN 1 AND 128",
            name="ck_package_observations_deployment_id_length",
        ),
        sa.CheckConstraint(
            _lower_hex("release_digest", 64),
            name="ck_package_observations_release_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("observation_digest", 64),
            name="ck_package_observations_observation_digest",
        ),
        sa.CheckConstraint(
            "state IN ('unknown', 'prepared', 'active', 'healthy', 'stopped', "
            "'failed', 'rolling-back')",
            name="ck_package_observations_state",
        ),
        sa.CheckConstraint(
            "summary IS NULL OR length(CAST(summary AS TEXT)) <= 8192",
            name="ck_package_observations_summary_size",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["agent_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "node_id",
            "deployment_id",
            "release_digest",
            "observation_digest",
            name="uq_package_observations_identity",
        ),
    )
    for name, columns in (
        ("ix_package_observations_node_id", ["node_id"]),
        ("ix_package_observations_deployment_id", ["deployment_id"]),
        ("ix_package_observations_state", ["state"]),
        ("ix_package_observations_observed_at", ["observed_at"]),
    ):
        op.create_index(name, "package_observations", columns, unique=False)


def downgrade() -> None:
    for name, table in (
        ("ix_package_observations_observed_at", "package_observations"),
        ("ix_package_observations_state", "package_observations"),
        ("ix_package_observations_deployment_id", "package_observations"),
        ("ix_package_observations_node_id", "package_observations"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("package_observations")

    for name, table in (
        ("ix_package_rollout_nodes_created_at", "package_rollout_nodes"),
        ("ix_package_rollout_nodes_state", "package_rollout_nodes"),
        ("ix_package_rollout_nodes_node_id", "package_rollout_nodes"),
        ("ix_package_rollout_nodes_rollout_id", "package_rollout_nodes"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("package_rollout_nodes")

    for name, table in (
        ("ix_package_rollouts_created_at", "package_rollouts"),
        ("ix_package_rollouts_state", "package_rollouts"),
        ("ix_package_rollouts_release_digest", "package_rollouts"),
        ("ix_package_rollouts_deployment_id", "package_rollouts"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("package_rollouts")

    for name, table in (
        ("ix_package_validation_runs_created_at", "package_validation_runs"),
        ("ix_package_validation_runs_state", "package_validation_runs"),
        ("ix_package_validation_runs_release_digest", "package_validation_runs"),
        ("ix_package_validation_runs_resolution_id", "package_validation_runs"),
        ("ix_package_validation_runs_candidate_id", "package_validation_runs"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("package_validation_runs")

    for name, table in (
        ("ix_package_resolutions_created_at", "package_resolutions"),
        ("ix_package_resolutions_release_digest", "package_resolutions"),
        ("ix_package_resolutions_state", "package_resolutions"),
        ("ix_package_resolutions_candidate_id", "package_resolutions"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("package_resolutions")

    for name, table in (
        ("ix_package_candidates_created_at", "package_candidates"),
        ("ix_package_candidates_last_seen_at", "package_candidates"),
        ("ix_package_candidates_first_seen_at", "package_candidates"),
        ("ix_package_candidates_state", "package_candidates"),
        ("ix_package_candidates_family_id", "package_candidates"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_table("package_candidates")
