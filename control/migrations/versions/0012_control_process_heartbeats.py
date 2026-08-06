"""Persist generation-bound control worker scheduler heartbeats."""

import sqlalchemy as sa
from alembic import op

revision = "0012_control_process_heartbeats"
down_revision = "0011_update_rollouts"
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
        "control_process_heartbeats",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("process_kind", sa.String(length=16), nullable=False),
        sa.Column("generation_id", sa.String(length=128), nullable=False),
        sa.Column("release_digest", sa.String(length=71), nullable=False),
        sa.Column("build_digest", sa.String(length=71), nullable=False),
        sa.Column("start_nonce", sa.String(length=64), nullable=False),
        sa.Column("loop_sequence", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "process_kind = 'worker'",
            name="ck_control_process_heartbeats_process_kind",
        ),
        sa.CheckConstraint(
            "length(generation_id) BETWEEN 1 AND 128",
            name="ck_control_process_heartbeats_generation_id_length",
        ),
        sa.CheckConstraint(
            "length(release_digest) = 71 AND "
            "substr(release_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(release_digest, 8, 64)', 64)})",
            name="ck_control_process_heartbeats_release_digest",
        ),
        sa.CheckConstraint(
            "length(build_digest) = 71 AND "
            "substr(build_digest, 1, 7) = 'sha256:' AND "
            f"({_lower_hex('substr(build_digest, 8, 64)', 64)})",
            name="ck_control_process_heartbeats_build_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("start_nonce", 64),
            name="ck_control_process_heartbeats_start_nonce",
        ),
        sa.CheckConstraint(
            "loop_sequence >= 1",
            name="ck_control_process_heartbeats_loop_sequence",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "process_kind",
            "start_nonce",
            name="uq_control_process_heartbeats_process_start",
        ),
    )
    op.create_index(
        "ix_control_process_heartbeats_completed_at",
        "control_process_heartbeats",
        ["completed_at"],
        unique=False,
    )
    op.create_index(
        "ix_control_process_heartbeats_generation_id",
        "control_process_heartbeats",
        ["generation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_process_heartbeats_generation_id",
        table_name="control_process_heartbeats",
    )
    op.drop_index(
        "ix_control_process_heartbeats_completed_at",
        table_name="control_process_heartbeats",
    )
    op.drop_table("control_process_heartbeats")
