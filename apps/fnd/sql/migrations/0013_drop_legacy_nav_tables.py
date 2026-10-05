"""Drop empty, unprefixed navigation tables left by an earlier setup.

The active navigation schema is the tenant-aware ``fnd_nav_*`` catalog. These
unprefixed tables are not referenced by the application. Refuse to drop them
if any contain data so an unexpected legacy deployment cannot lose records.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_drop_legacy_nav"
down_revision = "0012_default_roles"
branch_labels = None
depends_on = None


LEGACY_TABLES = (
    # Child tables first, followed by referenced tables.
    "nav_menu_items",
    "nav_functions",
    "nav_menus",
    "nav_pages",
    "nav_versions",
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    tables = [name for name in LEGACY_TABLES if name in existing]

    # Preflight every table before dropping any of them. PostgreSQL runs this
    # revision transactionally, but the explicit check gives a clear error
    # rather than making an assumption about a legacy database's contents.
    for table in tables:
        count = bind.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
        if count:
            raise RuntimeError(
                f"Refusing to drop non-empty legacy table {table!r} ({count} rows)"
            )

    for table in tables:
        op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError(
        "The unprefixed navigation tables were empty and are not restored by downgrade."
    )
