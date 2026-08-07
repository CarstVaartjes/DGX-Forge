"""Allow immutable database recipe revisions to authorize package rollouts."""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0016_recipe_deployment_authority"
down_revision = "0015_recipe_catalog"
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


def _legacy_authority(base_commit: str, deployment: str, release: str) -> str:
    raw = json.dumps(
        {
            "base_commit": base_commit,
            "deployment_digest": deployment,
            "release_digest": release,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def upgrade() -> None:
    with op.batch_alter_table("package_rollouts") as batch:
        batch.add_column(sa.Column("recipe_revision_id", sa.String(36)))
        batch.add_column(sa.Column("authority_digest", sa.String(64)))
        batch.create_foreign_key(
            "fk_package_rollouts_recipe_revision",
            "local_recipe_revisions",
            ["recipe_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_package_rollouts_recipe_revision_id", ["recipe_revision_id"]
        )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id,base_commit,deployment_digest,release_digest "
            "FROM package_rollouts"
        )
    ).mappings()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE package_rollouts SET authority_digest=:authority WHERE id=:id"
            ),
            {
                "id": row["id"],
                "authority": _legacy_authority(
                    row["base_commit"], row["deployment_digest"], row["release_digest"]
                ),
            },
        )

    with op.batch_alter_table("package_rollouts") as batch:
        batch.drop_constraint("ck_package_rollouts_base_commit_length", type_="check")
        batch.drop_constraint(
            "uq_package_rollouts_deployment_release_commit_plan", type_="unique"
        )
        batch.alter_column(
            "base_commit", existing_type=sa.String(128), nullable=True
        )
        batch.alter_column(
            "authority_digest", existing_type=sa.String(64), nullable=False
        )
        batch.create_check_constraint(
            "ck_package_rollouts_base_commit_length",
            "base_commit IS NULL OR length(base_commit) BETWEEN 40 AND 128",
        )
        batch.create_check_constraint(
            "ck_package_rollouts_authority_digest", _lower_hex("authority_digest", 64)
        )
        batch.create_check_constraint(
            "ck_package_rollouts_authority_kind",
            "(base_commit IS NOT NULL AND recipe_revision_id IS NULL) OR "
            "(base_commit IS NULL AND recipe_revision_id IS NOT NULL)",
        )
        batch.create_unique_constraint(
            "uq_package_rollouts_deployment_authority_plan",
            ["deployment_id", "authority_digest", "plan_digest"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    recipe_rows = connection.execute(
        sa.text(
            "SELECT count(*) FROM package_rollouts WHERE recipe_revision_id IS NOT NULL"
        )
    ).scalar_one()
    if recipe_rows:
        raise RuntimeError("cannot downgrade while recipe-authorized rollouts exist")
    with op.batch_alter_table("package_rollouts") as batch:
        batch.drop_constraint(
            "uq_package_rollouts_deployment_authority_plan", type_="unique"
        )
        batch.drop_constraint("ck_package_rollouts_authority_kind", type_="check")
        batch.drop_constraint("ck_package_rollouts_authority_digest", type_="check")
        batch.drop_constraint("ck_package_rollouts_base_commit_length", type_="check")
        batch.alter_column(
            "base_commit", existing_type=sa.String(128), nullable=False
        )
        batch.create_check_constraint(
            "ck_package_rollouts_base_commit_length",
            "length(base_commit) BETWEEN 40 AND 128",
        )
        batch.create_unique_constraint(
            "uq_package_rollouts_deployment_release_commit_plan",
            ["deployment_id", "release_digest", "base_commit", "plan_digest"],
        )
        batch.drop_index("ix_package_rollouts_recipe_revision_id")
        batch.drop_constraint("fk_package_rollouts_recipe_revision", type_="foreignkey")
        batch.drop_column("authority_digest")
        batch.drop_column("recipe_revision_id")
