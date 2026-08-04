"""Persist one-time Spark agent enrollment evidence and certificates."""

from alembic import op
import sqlalchemy as sa


revision = "0004_agent_enrollment"
down_revision = "0003_retry_disposition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_enrollment_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index("ix_agent_enrollment_grants_node_id", "agent_enrollment_grants", ["node_id"])
    op.create_index("ix_agent_enrollment_grants_created_at", "agent_enrollment_grants", ["created_at"])
    op.create_index("ix_agent_enrollment_grants_expires_at", "agent_enrollment_grants", ["expires_at"])
    op.create_table(
        "agent_enrollments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("csr_pem", sa.Text(), nullable=False),
        sa.Column("csr_public_key_pem", sa.Text(), nullable=False),
        sa.Column("csr_public_key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("host_key_fingerprint", sa.String(length=512), nullable=False),
        sa.Column("hardware_fingerprint", sa.String(length=512), nullable=False),
        sa.Column("agent_digest", sa.String(length=128), nullable=False),
        sa.Column("boot_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_actor", sa.String(length=200)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("certificate_pem", sa.Text()),
        sa.Column("chain_pem", sa.Text()),
        sa.Column("certificate_serial", sa.String(length=128)),
        sa.Column("certificate_fingerprint", sa.String(length=128)),
        sa.Column("certificate_not_before", sa.DateTime(timezone=True)),
        sa.Column("certificate_not_after", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["grant_id"], ["agent_enrollment_grants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("certificate_fingerprint"),
        sa.UniqueConstraint("certificate_serial"),
        sa.UniqueConstraint("grant_id"),
    )
    op.create_index("ix_agent_enrollments_node_id", "agent_enrollments", ["node_id"])
    op.create_index("ix_agent_enrollments_state", "agent_enrollments", ["state"])
    op.create_index("ix_agent_enrollments_created_at", "agent_enrollments", ["created_at"])
    op.add_column("agent_certificates", sa.Column("ca_revoked_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("agent_certificates", "ca_revoked_at")
    op.drop_index("ix_agent_enrollments_created_at", table_name="agent_enrollments")
    op.drop_index("ix_agent_enrollments_state", table_name="agent_enrollments")
    op.drop_index("ix_agent_enrollments_node_id", table_name="agent_enrollments")
    op.drop_table("agent_enrollments")
    op.drop_index("ix_agent_enrollment_grants_expires_at", table_name="agent_enrollment_grants")
    op.drop_index("ix_agent_enrollment_grants_created_at", table_name="agent_enrollment_grants")
    op.drop_index("ix_agent_enrollment_grants_node_id", table_name="agent_enrollment_grants")
    op.drop_table("agent_enrollment_grants")
