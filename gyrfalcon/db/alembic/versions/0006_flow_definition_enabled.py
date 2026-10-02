"""Add an enabled switch to visual flow definitions."""

from alembic import op
import sqlalchemy as sa


revision = "0006_flow_definition_enabled"
down_revision = "0005_retire_legacy_flow_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fnd_flow_dt_definitions",
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("fnd_flow_dt_definitions", "enabled")
