"""Persist durable reconciliation execution and route publication state."""

import sqlalchemy as sa
from alembic import op

revision = "0009_reconciliation_execution"
down_revision = "0008_resolved_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("reconciliation_id", sa.String(length=36)))
        batch.create_foreign_key(
            "fk_jobs_reconciliation_id_reconciliations",
            "reconciliations",
            ["reconciliation_id"],
            ["id"],
        )
        batch.create_index(
            "ix_jobs_reconciliation_id", ["reconciliation_id"], unique=True
        )

    op.create_table(
        "agent_presence",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("certificate_serial", sa.String(length=128), nullable=False),
        sa.Column(
            "certificate_fingerprint", sa.String(length=128), nullable=False
        ),
        sa.Column("management_address", sa.String(length=45), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(management_address) BETWEEN 2 AND 45",
            name="ck_agent_presence_management_address_length",
        ),
        sa.ForeignKeyConstraint(
            ["certificate_serial"], ["agent_certificates.serial"]
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["agent_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index(
        "ix_agent_presence_certificate_serial",
        "agent_presence",
        ["certificate_serial"],
        unique=False,
    )
    op.create_index(
        "ix_agent_presence_observed_at",
        "agent_presence",
        ["observed_at"],
        unique=False,
    )

    op.create_table(
        "reconciliation_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reconciliation_id", sa.String(length=36), nullable=False),
        sa.Column("graph_operation_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("agent_operation_id", sa.String(length=36)),
        sa.Column("expected_payload_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("result_digest", sa.String(length=64)),
        sa.Column("evidence_digest", sa.String(length=64)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("compensated_graph_operation_id", sa.String(length=128)),
        sa.CheckConstraint(
            "length(graph_operation_id) BETWEEN 1 AND 128",
            name="ck_reconciliation_operations_graph_operation_id_length",
        ),
        sa.CheckConstraint(
            "role IN ('primary', 'compensation')",
            name="ck_reconciliation_operations_role",
        ),
        sa.CheckConstraint(
            "state IN ('planned', 'queued', 'running', 'succeeded', "
            "'accepted', 'failed', 'waiting-for-operator', 'compensating', "
            "'compensated', 'uncertain')",
            name="ck_reconciliation_operations_state",
        ),
        sa.CheckConstraint(
            "length(expected_payload_digest) = 64",
            name="ck_reconciliation_operations_expected_payload_digest_length",
        ),
        sa.CheckConstraint(
            "result_digest IS NULL OR length(result_digest) = 64",
            name="ck_reconciliation_operations_result_digest_length",
        ),
        sa.CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_reconciliation_operations_evidence_digest_length",
        ),
        sa.CheckConstraint(
            "compensated_graph_operation_id IS NULL OR "
            "length(compensated_graph_operation_id) BETWEEN 1 AND 128",
            name="ck_reconciliation_operations_compensated_id_length",
        ),
        sa.ForeignKeyConstraint(
            ["agent_operation_id"], ["agent_operations.id"]
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"], ["reconciliations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reconciliation_id",
            "graph_operation_id",
            "role",
            name="uq_reconciliation_operation_graph_role",
        ),
    )
    op.create_index(
        "ix_reconciliation_operations_agent_operation_id",
        "reconciliation_operations",
        ["agent_operation_id"],
        unique=True,
    )
    op.create_index(
        "ix_reconciliation_operations_reconciliation_id",
        "reconciliation_operations",
        ["reconciliation_id"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_operations_state",
        "reconciliation_operations",
        ["state"],
        unique=False,
    )

    op.create_table(
        "reconciliation_cancellations",
        sa.Column("reconciliation_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('requested', 'withdrawal-pending', 'withdrawn', "
            "'processing', 'compensating', 'completed', "
            "'waiting-for-operator')",
            name="ck_reconciliation_cancellations_state",
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 1024",
            name="ck_reconciliation_cancellations_reason_length",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"], ["reconciliations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("reconciliation_id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index(
        "ix_reconciliation_cancellations_state",
        "reconciliation_cancellations",
        ["state"],
        unique=False,
    )

    op.create_table(
        "route_publications",
        sa.Column("reconciliation_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.BigInteger()),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64)),
        sa.Column("route_digest", sa.String(length=64)),
        sa.Column("litellm_digest", sa.String(length=64)),
        sa.Column("bundle_digest", sa.String(length=64)),
        sa.Column("activation_marker", sa.JSON()),
        sa.Column("activation_marker_digest", sa.String(length=64)),
        sa.Column("lease_issued_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('withdrawal-pending', 'routes-withdrawn', "
            "'publication-pending', 'completed', 'failed')",
            name="ck_route_publications_state",
        ),
        sa.CheckConstraint(
            "generation IS NULL OR generation >= 0",
            name="ck_route_publications_generation",
        ),
        sa.CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_route_publications_plan_digest_length",
        ),
        sa.CheckConstraint(
            "evidence_digest IS NULL OR length(evidence_digest) = 64",
            name="ck_route_publications_evidence_digest_length",
        ),
        sa.CheckConstraint(
            "route_digest IS NULL OR length(route_digest) = 64",
            name="ck_route_publications_route_digest_length",
        ),
        sa.CheckConstraint(
            "litellm_digest IS NULL OR length(litellm_digest) = 64",
            name="ck_route_publications_litellm_digest_length",
        ),
        sa.CheckConstraint(
            "bundle_digest IS NULL OR length(bundle_digest) = 64",
            name="ck_route_publications_bundle_digest_length",
        ),
        sa.CheckConstraint(
            "activation_marker_digest IS NULL OR "
            "length(activation_marker_digest) = 64",
            name="ck_route_publications_activation_marker_digest_length",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR "
            "(lease_issued_at IS NOT NULL AND lease_expires_at > lease_issued_at)",
            name="ck_route_publications_lease_window",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"], ["reconciliations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("reconciliation_id"),
    )
    op.create_index(
        "ix_route_publications_generation",
        "route_publications",
        ["generation"],
        unique=True,
    )
    op.create_index(
        "ix_route_publications_state",
        "route_publications",
        ["state"],
        unique=False,
    )

    op.create_table(
        "route_publication_owner",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("reconciliation_id", sa.String(length=36)),
        sa.Column("owner_generation", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "singleton_id = 1",
            name="ck_route_publication_owner_singleton",
        ),
        sa.CheckConstraint(
            "owner_generation >= 0",
            name="ck_route_publication_owner_generation",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"],
            ["reconciliations.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
        sa.UniqueConstraint("reconciliation_id"),
    )
    op.bulk_insert(
        sa.table(
            "route_publication_owner",
            sa.column("singleton_id", sa.Integer()),
            sa.column("reconciliation_id", sa.String(length=36)),
            sa.column("owner_generation", sa.BigInteger()),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "singleton_id": 1,
                "reconciliation_id": None,
                "owner_generation": 0,
                "updated_at": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("route_publication_owner")
    op.drop_index(
        "ix_route_publications_state", table_name="route_publications"
    )
    op.drop_index(
        "ix_route_publications_generation", table_name="route_publications"
    )
    op.drop_table("route_publications")

    op.drop_index(
        "ix_reconciliation_cancellations_state",
        table_name="reconciliation_cancellations",
    )
    op.drop_table("reconciliation_cancellations")

    op.drop_index(
        "ix_reconciliation_operations_state",
        table_name="reconciliation_operations",
    )
    op.drop_index(
        "ix_reconciliation_operations_reconciliation_id",
        table_name="reconciliation_operations",
    )
    op.drop_index(
        "ix_reconciliation_operations_agent_operation_id",
        table_name="reconciliation_operations",
    )
    op.drop_table("reconciliation_operations")

    op.drop_index(
        "ix_agent_presence_observed_at", table_name="agent_presence"
    )
    op.drop_index(
        "ix_agent_presence_certificate_serial", table_name="agent_presence"
    )
    op.drop_table("agent_presence")

    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_reconciliation_id")
        batch.drop_constraint(
            "fk_jobs_reconciliation_id_reconciliations", type_="foreignkey"
        )
        batch.drop_column("reconciliation_id")
