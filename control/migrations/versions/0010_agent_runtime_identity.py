"""Persist the authenticated running GPU node agent release identity."""

import sqlalchemy as sa
from alembic import op

revision = "0010_agent_runtime_identity"
down_revision = "0009_reconciliation_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_nodes") as batch:
        batch.add_column(sa.Column("platform_version", sa.String(length=32)))
        batch.add_column(sa.Column("build_digest", sa.String(length=71)))
        batch.add_column(sa.Column("active_slot", sa.String(length=1)))
        batch.add_column(sa.Column("agent_sha256", sa.String(length=64)))
        batch.add_column(sa.Column("supervisor_generation", sa.Integer()))


def downgrade() -> None:
    with op.batch_alter_table("agent_nodes") as batch:
        batch.drop_column("supervisor_generation")
        batch.drop_column("agent_sha256")
        batch.drop_column("active_slot")
        batch.drop_column("build_digest")
        batch.drop_column("platform_version")
