"""Persist fenced Spark agent operations."""

from alembic import op
import sqlalchemy as sa


revision = "0002_agent_operations"
down_revision = "0001_operational_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_nodes",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("protocol_version", sa.Integer()),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_table(
        "agent_certificates",
        sa.Column("serial", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["node_id"], ["agent_nodes.node_id"]),
        sa.PrimaryKeyConstraint("serial"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index("ix_agent_certificates_node_id", "agent_certificates", ["node_id"])
    op.create_table(
        "agent_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("parent_job_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("base_commit", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("current_attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["agent_nodes.node_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_operations_parent_job_id", "agent_operations", ["parent_job_id"])
    op.create_index("ix_agent_operations_node_id", "agent_operations", ["node_id"])
    op.create_index("ix_agent_operations_state", "agent_operations", ["state"])
    op.create_index("ix_agent_operations_created_at", "agent_operations", ["created_at"])
    op.create_table(
        "agent_operation_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("fence", sa.String(length=36), nullable=False),
        sa.Column("lease_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_certificate_serial", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.ForeignKeyConstraint(["operation_id"], ["agent_operations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_certificate_serial"], ["agent_certificates.serial"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", "attempt"),
        sa.UniqueConstraint("fence"),
    )
    op.create_index("ix_agent_operation_attempts_operation_id", "agent_operation_attempts", ["operation_id"])
    op.create_index("ix_agent_operation_attempts_lease_deadline", "agent_operation_attempts", ["lease_deadline"])
    op.create_index(
        "ix_agent_operation_attempts_agent_certificate_serial",
        "agent_operation_attempts",
        ["agent_certificate_serial"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_operation_attempts_agent_certificate_serial", table_name="agent_operation_attempts")
    op.drop_index("ix_agent_operation_attempts_lease_deadline", table_name="agent_operation_attempts")
    op.drop_index("ix_agent_operation_attempts_operation_id", table_name="agent_operation_attempts")
    op.drop_table("agent_operation_attempts")
    op.drop_index("ix_agent_operations_created_at", table_name="agent_operations")
    op.drop_index("ix_agent_operations_state", table_name="agent_operations")
    op.drop_index("ix_agent_operations_node_id", table_name="agent_operations")
    op.drop_index("ix_agent_operations_parent_job_id", table_name="agent_operations")
    op.drop_table("agent_operations")
    op.drop_index("ix_agent_certificates_node_id", table_name="agent_certificates")
    op.drop_table("agent_certificates")
    op.drop_table("agent_nodes")
