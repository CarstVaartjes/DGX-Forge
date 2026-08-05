"""Persist complete resolved reconciliation plans by content digest."""

import sqlalchemy as sa
from alembic import op

revision = "0008_resolved_plan"
down_revision = "0007_issued_revocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reconciliations") as batch:
        batch.add_column(sa.Column("plan_digest", sa.String(length=64)))
        batch.add_column(sa.Column("resolved_plan", sa.JSON()))
        batch.create_index(
            "ix_reconciliations_plan_digest", ["plan_digest"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("reconciliations") as batch:
        batch.drop_index("ix_reconciliations_plan_digest")
        batch.drop_column("resolved_plan")
        batch.drop_column("plan_digest")
