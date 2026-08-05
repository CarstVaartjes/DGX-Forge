"""Persist complete resolved reconciliation plans by content digest."""

import sqlalchemy as sa
from alembic import op

revision = "0008_resolved_plan"
down_revision = "0007_issued_revocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_completion_generation",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("last_generation", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    op.bulk_insert(
        sa.table(
            "reconciliation_completion_generation",
            sa.column("singleton_id", sa.Integer()),
            sa.column("last_generation", sa.BigInteger()),
        ),
        [{"singleton_id": 1, "last_generation": 0}],
    )
    with op.batch_alter_table("reconciliations") as batch:
        batch.add_column(sa.Column("plan_digest", sa.String(length=64)))
        batch.add_column(sa.Column("resolved_plan", sa.JSON()))
        batch.add_column(sa.Column("completion_generation", sa.BigInteger()))
        batch.create_index(
            "ix_reconciliations_plan_digest", ["plan_digest"], unique=True
        )
        batch.create_index(
            "ix_reconciliations_completion_generation",
            ["completion_generation"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("reconciliations") as batch:
        batch.drop_index("ix_reconciliations_completion_generation")
        batch.drop_index("ix_reconciliations_plan_digest")
        batch.drop_column("completion_generation")
        batch.drop_column("resolved_plan")
        batch.drop_column("plan_digest")
    op.drop_table("reconciliation_completion_generation")
