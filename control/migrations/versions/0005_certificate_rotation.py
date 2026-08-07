"""Stage and atomically activate GPU node agent credential generations."""

import sqlalchemy as sa
from alembic import op

revision = "0005_certificate_rotation"
down_revision = "0004_agent_enrollment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_certificates") as batch:
        batch.add_column(
            sa.Column("state", sa.String(length=24), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("generation", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("certificate_pem", sa.Text()))
        batch.add_column(sa.Column("chain_pem", sa.Text()))
        batch.add_column(sa.Column("csr_public_key_fingerprint", sa.String(length=64)))

    # Historical renewal retained revoked certificate rows. Give every row a
    # deterministic per-node generation before imposing uniqueness so those
    # databases remain upgradeable; all pre-rotation states remain "active"
    # as required, while revoked_at continues to deny old identities.
    op.execute(sa.text("""
        UPDATE agent_certificates AS target
        SET generation = (
            SELECT COUNT(*)
            FROM agent_certificates AS candidate
            WHERE candidate.node_id = target.node_id
              AND (
                  candidate.not_before < target.not_before
                  OR (
                      candidate.not_before = target.not_before
                      AND candidate.serial <= target.serial
                  )
              )
        )
    """))

    with op.batch_alter_table("agent_certificates") as batch:
        batch.alter_column(
            "generation",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
        batch.create_unique_constraint(
            "uq_agent_certificates_node_generation", ["node_id", "generation"]
        )

    op.create_table(
        "agent_certificate_rotations",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("source_serial", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("csr_pem", sa.Text(), nullable=False),
        sa.Column(
            "csr_public_key_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "provider_request_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["agent_nodes.node_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint("provider_request_id"),
    )
    op.create_index(
        op.f("ix_agent_certificate_rotations_state"),
        "agent_certificate_rotations",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    # The previous application has no state discriminator and authenticates a
    # valid certificate when revoked_at is NULL. Preserve denial for staged or
    # locally retired rows before removing the discriminator.
    op.execute(sa.text("""
        UPDATE agent_certificates
        SET revoked_at = CURRENT_TIMESTAMP
        WHERE state <> 'active' AND revoked_at IS NULL
    """))
    op.drop_index(
        op.f("ix_agent_certificate_rotations_state"),
        table_name="agent_certificate_rotations",
    )
    op.drop_table("agent_certificate_rotations")
    with op.batch_alter_table("agent_certificates") as batch:
        batch.drop_constraint(
            "uq_agent_certificates_node_generation", type_="unique"
        )
        batch.drop_column("csr_public_key_fingerprint")
        batch.drop_column("chain_pem")
        batch.drop_column("certificate_pem")
        batch.drop_column("generation")
        batch.drop_column("state")
