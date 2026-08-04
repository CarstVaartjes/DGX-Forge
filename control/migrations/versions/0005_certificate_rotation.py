"""Stage and atomically activate Spark agent credential generations."""

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


def downgrade() -> None:
    with op.batch_alter_table("agent_certificates") as batch:
        batch.drop_constraint(
            "uq_agent_certificates_node_generation", type_="unique"
        )
        batch.drop_column("csr_public_key_fingerprint")
        batch.drop_column("chain_pem")
        batch.drop_column("certificate_pem")
        batch.drop_column("generation")
        batch.drop_column("state")
