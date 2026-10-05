"""Persist stable business context separately from execution checkpoints."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_flow_runtime_context"
down_revision = "0009_flow_activity_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fnd_flow_rt_contexts",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), server_default="default", nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("flow_context", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.Column("updated_at", sa.Float(precision=53), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "environment_id", "run_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment_id", "run_id"],
            ["fnd_flow_rt_statuses.tenant_id", "fnd_flow_rt_statuses.environment_id", "fnd_flow_rt_statuses.id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("fnd_flow_rt_contexts")
