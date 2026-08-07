"""Persist exact local publisher test reports."""

import sqlalchemy as sa
from alembic import op

revision = "0020_recipe_catalog_bridge"
down_revision = "0019_rust_agent_migration"
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
    with op.batch_alter_table("recipe_global_links") as batch:
        batch.create_unique_constraint(
            "uq_recipe_global_link_identity", ["global_publisher", "global_slug"]
        )
    op.create_table(
        "recipe_test_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "recipe_revision_id",
            sa.String(36),
            sa.ForeignKey("local_recipe_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("report", sa.JSON, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "recipe_revision_id", "report_sha256", name="uq_recipe_test_report_digest"
        ),
        sa.CheckConstraint(
            _lower_hex("report_sha256", 64), name="ck_recipe_test_reports_digest"
        ),
    )
    op.create_index(
        "ix_recipe_test_reports_recipe_revision_id",
        "recipe_test_reports",
        ["recipe_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recipe_test_reports_recipe_revision_id",
        table_name="recipe_test_reports",
    )
    op.drop_table("recipe_test_reports")
    with op.batch_alter_table("recipe_global_links") as batch:
        batch.drop_constraint("uq_recipe_global_link_identity", type_="unique")
