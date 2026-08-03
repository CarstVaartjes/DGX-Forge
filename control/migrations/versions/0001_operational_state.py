"""Create operational state tables."""

from alembic import op

from dgx_control.models import Base

revision = "0001_operational_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
