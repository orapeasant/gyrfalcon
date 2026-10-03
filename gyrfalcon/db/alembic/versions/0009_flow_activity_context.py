"""Persist the exact context visible to each Activity visit."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_flow_activity_context"
down_revision = "0008_flow_publish_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fnd_flow_rt_node_statuses",
        sa.Column("context_snapshot", postgresql.JSONB()),
    )


def downgrade() -> None:
    op.drop_column("fnd_flow_rt_node_statuses", "context_snapshot")
