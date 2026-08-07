"""Require Rust for new enrollments and track legacy migration."""

import sqlalchemy as sa
from alembic import op

revision = "0019_rust_agent_migration"
down_revision = "0018_agent_inventory_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_enrollment_grants") as batch:
        batch.add_column(
            sa.Column(
                "purpose",
                sa.String(24),
                nullable=False,
                server_default="new-node",
            )
        )
        batch.create_check_constraint(
            "ck_agent_enrollment_grants_purpose",
            "purpose IN ('new-node', 'rust-migration')",
        )
    with op.batch_alter_table("agent_enrollments") as batch:
        batch.add_column(sa.Column("certificate_generation", sa.Integer()))
    op.execute(
        "UPDATE agent_enrollments SET certificate_generation=1 "
        "WHERE certificate_serial IS NOT NULL"
    )
    with op.batch_alter_table("agent_nodes") as batch:
        batch.add_column(
            sa.Column(
                "agent_implementation",
                sa.String(16),
                nullable=False,
                server_default="pending",
            )
        )
        batch.add_column(
            sa.Column(
                "migration_state",
                sa.String(16),
                nullable=False,
                server_default="required",
            )
        )
    op.execute(
        "UPDATE agent_nodes SET agent_implementation='python', migration_state='required'"
    )
    with op.batch_alter_table("agent_nodes") as batch:
        batch.create_check_constraint(
            "ck_agent_nodes_implementation",
            "agent_implementation IN ('pending', 'python', 'rust')",
        )
        batch.create_check_constraint(
            "ck_agent_nodes_migration_state",
            "migration_state IN ('required', 'complete')",
        )
        batch.create_check_constraint(
            "ck_agent_nodes_migration_consistency",
            "(agent_implementation = 'rust' AND migration_state = 'complete') OR "
            "(agent_implementation IN ('pending', 'python') AND migration_state = 'required')",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_nodes") as batch:
        batch.drop_constraint(
            "ck_agent_nodes_migration_consistency", type_="check"
        )
        batch.drop_constraint("ck_agent_nodes_migration_state", type_="check")
        batch.drop_constraint("ck_agent_nodes_implementation", type_="check")
        batch.drop_column("migration_state")
        batch.drop_column("agent_implementation")
    with op.batch_alter_table("agent_enrollment_grants") as batch:
        batch.drop_constraint(
            "ck_agent_enrollment_grants_purpose", type_="check"
        )
        batch.drop_column("purpose")
    with op.batch_alter_table("agent_enrollments") as batch:
        batch.drop_column("certificate_generation")
