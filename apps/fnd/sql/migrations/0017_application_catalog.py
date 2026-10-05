"""Create the FND application catalog and link navigation records to apps."""

from __future__ import annotations

import time
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017_application_catalog"
down_revision = "0016_group_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fnd_applications",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("command", sa.Text(), nullable=False, server_default=""),
        sa.Column("args", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("env", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("tags", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("version", sa.Text(), nullable=False, server_default="1.0.0"),
        sa.Column("source_sessions", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("config", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.Column("updated_at", sa.Float(precision=53), nullable=False),
    )
    op.create_index("idx_fnd_applications_enabled", "fnd_applications", ["enabled", "name"])

    app_id = uuid.uuid4().hex
    now = time.time()
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO fnd_applications "
        "(id, code, name, description, enabled, command, args, env, tags, version, "
        "source_sessions, config, created_at, updated_at) "
        "VALUES (:id, 'FND', 'Foundation', 'Foundation application', true, '', "
        "'[]'::jsonb, '{}'::jsonb, '[]'::jsonb, '1.0.0', '[]'::jsonb, '{}'::jsonb, :now, :now)"
    ), {"id": app_id, "now": now})

    for table in ("fnd_nav_functions", "fnd_nav_menus", "fnd_nav_menu_items"):
        op.add_column(table, sa.Column("application_id", sa.Text(), nullable=True))
        bind.execute(sa.text(
            f"UPDATE {table} SET application_id = :application_id "
            "WHERE application_id IS NULL"
        ), {"application_id": app_id})
        op.alter_column(table, "application_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_application", table, "fnd_applications",
            ["application_id"], ["id"],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Application associations contain operator-managed data and are not removed by downgrade."
    )
