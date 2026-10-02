"""Track whether a visual flow is currently published."""

from alembic import op
import sqlalchemy as sa


revision = "0008_flow_publish_state"
down_revision = "0007_drop_migration_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fnd_flow_dt_definitions",
        sa.Column("published", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("""
        UPDATE fnd_flow_dt_definitions AS definition
        SET published = TRUE
        WHERE EXISTS (
            SELECT 1 FROM fnd_flow_dt_definition_versions AS version
            WHERE version.tenant_id = definition.tenant_id
              AND version.definition_id = definition.id
        )
    """)


def downgrade() -> None:
    op.drop_column("fnd_flow_dt_definitions", "published")
