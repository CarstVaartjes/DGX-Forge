"""Retain bounded CA-revocation evidence independently of agent nodes."""

import sqlalchemy as sa
from alembic import op

revision = "0007_issued_revocations"
down_revision = "0006_reconciliation_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_issued_certificate_revocations",
        sa.Column("serial", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("provider_request_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ca_revoked_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("serial"),
        sa.UniqueConstraint("provider_request_id"),
    )
    op.create_index(
        op.f("ix_agent_issued_certificate_revocations_node_id"),
        "agent_issued_certificate_revocations",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_issued_certificate_revocations_state"),
        "agent_issued_certificate_revocations",
        ["state"],
        unique=False,
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION vonk_prevent_agent_node_delete()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'agent nodes are immutable; retire instead'
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$
        """))
        op.execute(sa.text("""
            CREATE TRIGGER vonk_agent_nodes_retire_only
            BEFORE DELETE ON agent_nodes
            FOR EACH ROW
            EXECUTE FUNCTION vonk_prevent_agent_node_delete()
        """))
    elif dialect == "sqlite":
        op.execute(sa.text("""
            CREATE TRIGGER vonk_agent_nodes_retire_only
            BEFORE DELETE ON agent_nodes
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'agent nodes are immutable; retire instead');
            END
        """))


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS vonk_agent_nodes_retire_only ON agent_nodes"
        ))
        op.execute(sa.text(
            "DROP FUNCTION IF EXISTS vonk_prevent_agent_node_delete()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS vonk_agent_nodes_retire_only"))
    op.drop_index(
        op.f("ix_agent_issued_certificate_revocations_state"),
        table_name="agent_issued_certificate_revocations",
    )
    op.drop_index(
        op.f("ix_agent_issued_certificate_revocations_node_id"),
        table_name="agent_issued_certificate_revocations",
    )
    op.drop_table("agent_issued_certificate_revocations")
