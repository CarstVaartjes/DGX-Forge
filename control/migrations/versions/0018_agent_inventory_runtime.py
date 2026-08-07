"""Record authenticated runtime and direct-fabric inventory evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0018_agent_inventory_runtime"
down_revision = "0017_admission_and_run_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_inventory_snapshots") as batch:
        batch.add_column(sa.Column("fabric_address", sa.String(45)))
        batch.add_column(sa.Column("fabric_bandwidth_mbps", sa.BigInteger))
        batch.add_column(
            sa.Column(
                "nvidia_driver_version",
                sa.String(256),
                nullable=False,
                server_default="unknown",
            )
        )
        batch.add_column(
            sa.Column(
                "container_runtime_version",
                sa.String(256),
                nullable=False,
                server_default="unknown",
            )
        )
        batch.create_check_constraint(
            "ck_inventory_fabric",
            "(fabric_address IS NULL AND fabric_bandwidth_mbps IS NULL) OR "
            "(fabric_address IS NOT NULL AND fabric_bandwidth_mbps>0)",
        )


def downgrade() -> None:
    with op.batch_alter_table("node_inventory_snapshots") as batch:
        batch.drop_constraint("ck_inventory_fabric", type_="check")
        batch.drop_column("container_runtime_version")
        batch.drop_column("nvidia_driver_version")
        batch.drop_column("fabric_bandwidth_mbps")
        batch.drop_column("fabric_address")
