"""Persist immutable reconciliation operation graphs."""

import sqlalchemy as sa
from alembic import op

revision = "0006_reconciliation_graph"
down_revision = "0005_certificate_rotation"
branch_labels = None
depends_on = None

_EMPTY_GRAPH = (
    '{"base_commit":"","nodes":[],"schema_version":1,"targets":[]}'
)
_EMPTY_GRAPH_DIGEST = (
    "5c061eb8dfce0a3f2bcbfbf06cb71d695c33e8f4269e17bfe5cd1cda0054cdc5"
)


def upgrade() -> None:
    with op.batch_alter_table("reconciliations") as batch:
        batch.add_column(
            sa.Column(
                "graph",
                sa.JSON(),
                nullable=False,
                server_default=_EMPTY_GRAPH,
            )
        )
        batch.add_column(
            sa.Column(
                "graph_digest",
                sa.String(length=64),
                nullable=False,
                server_default=_EMPTY_GRAPH_DIGEST,
            )
        )
        batch.add_column(
            sa.Column(
                "current_phase",
                sa.String(length=32),
                nullable=False,
                server_default="legacy",
            )
        )
        batch.add_column(
            sa.Column(
                "route_withdrawal_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("terminal_reason", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("reconciliations") as batch:
        batch.drop_column("terminal_reason")
        batch.drop_column("route_withdrawal_generation")
        batch.drop_column("current_phase")
        batch.drop_column("graph_digest")
        batch.drop_column("graph")
