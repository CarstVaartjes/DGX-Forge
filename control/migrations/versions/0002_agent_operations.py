"""Persist fenced Spark agent operations."""

from alembic import op

from dgx_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
)


revision = "0002_agent_operations"
down_revision = "0001_operational_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    AgentNode.__table__.create(bind=bind, checkfirst=True)
    AgentCertificate.__table__.create(bind=bind, checkfirst=True)
    AgentOperation.__table__.create(bind=bind, checkfirst=True)
    AgentOperationAttempt.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    AgentOperationAttempt.__table__.drop(bind=bind, checkfirst=True)
    AgentOperation.__table__.drop(bind=bind, checkfirst=True)
    AgentCertificate.__table__.drop(bind=bind, checkfirst=True)
    AgentNode.__table__.drop(bind=bind, checkfirst=True)
