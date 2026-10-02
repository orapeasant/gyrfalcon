"""Remove superseded migration bookkeeping tables not used by the app."""

from alembic import op
import sqlalchemy as sa


revision = "0007_drop_migration_tables"
down_revision = "0006_flow_definition_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("fnd_flow_schema_version", "fnd_migration_conflicts"):
        if sa.inspect(bind).has_table(table):
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError("Superseded migration bookkeeping tables are not restored")
