"""Persist digest-bound workload package preview/apply plans."""

import sqlalchemy as sa
from alembic import op

revision = "0014_package_action_plans"
down_revision = "0013_workload_packages"
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


def upgrade() -> None:
    op.create_table(
        "package_action_plans",
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=200)),
        sa.Column("result", sa.JSON()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_package_action_plans_plan_digest",
        ),
        sa.CheckConstraint(
            "action IN ('package.validate', 'package.promote', 'package.rollout', "
            "'package.rollback', 'package.repair', 'package.remove', 'package.gc')",
            name="ck_package_action_plans_action",
        ),
        sa.CheckConstraint(
            "state IN ('planned', 'applying', 'applied', 'expired', 'failed')",
            name="ck_package_action_plans_state",
        ),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 128",
            name="ck_package_action_plans_subject_length",
        ),
        sa.CheckConstraint(
            "length(CAST(request AS TEXT)) BETWEEN 2 AND 65536",
            name="ck_package_action_plans_request_size",
        ),
        sa.CheckConstraint(
            "result IS NULL OR length(CAST(result AS TEXT)) <= 16384",
            name="ck_package_action_plans_result_size",
        ),
        sa.PrimaryKeyConstraint("plan_digest"),
    )
    op.create_index(
        "ix_package_action_plans_action", "package_action_plans", ["action"]
    )
    op.create_index(
        "ix_package_action_plans_subject", "package_action_plans", ["subject"]
    )
    op.create_index(
        "ix_package_action_plans_state", "package_action_plans", ["state"]
    )
    op.create_index(
        "ix_package_action_plans_expires_at", "package_action_plans", ["expires_at"]
    )
    op.create_index(
        "ix_package_action_plans_created_at", "package_action_plans", ["created_at"]
    )


def downgrade() -> None:
    for name in (
        "ix_package_action_plans_created_at",
        "ix_package_action_plans_expires_at",
        "ix_package_action_plans_state",
        "ix_package_action_plans_subject",
        "ix_package_action_plans_action",
    ):
        op.drop_index(name, table_name="package_action_plans")
    op.drop_table("package_action_plans")
