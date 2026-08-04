"""Persist attempt-bound retry dispositions for agent operations."""

import sqlalchemy as sa
from alembic import op

revision = "0003_retry_disposition"
down_revision = "0002_agent_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_operations",
        sa.Column("retry_disposition", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "agent_operations",
        sa.Column("retry_disposition_attempt", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_operations", "retry_disposition_attempt")
    op.drop_column("agent_operations", "retry_disposition")
