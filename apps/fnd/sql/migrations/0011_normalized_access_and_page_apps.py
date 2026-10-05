"""Add app-owned page routing and normalized role/group assignment tables.

This is an additive compatibility revision. Legacy membership role JSON and
active_role remain until the application code and admin UI complete cutover.
"""

from __future__ import annotations

import json
import time

from alembic import op
import sqlalchemy as sa


revision = "0011_access_apps"
down_revision = "0010_flow_runtime_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fnd_nav_pages",
        sa.Column("app_key", sa.Text(), server_default="fnd", nullable=False),
    )
    op.create_index("idx_nav_pages_app", "fnd_nav_pages", ["app_key"])
    op.create_table(
        "fnd_nav_page_aliases",
        sa.Column("route", sa.Text(), primary_key=True),
        sa.Column("page_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.ForeignKeyConstraint(["page_key"], ["fnd_nav_pages.key"]),
    )
    op.create_index("idx_nav_page_aliases_page", "fnd_nav_page_aliases", ["page_key"])

    op.create_unique_constraint(
        "uq_auth_roles_tenant_id", "fnd_auth_roles", ["tenant_id", "id"]
    )
    op.create_table(
        "fnd_auth_user_roles",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "role_id"),
        sa.ForeignKeyConstraint(["user_id"], ["fnd_auth_users.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"], ["fnd_auth_roles.tenant_id", "fnd_auth_roles.id"]
        ),
    )
    op.create_index("idx_auth_user_roles_role", "fnd_auth_user_roles", ["tenant_id", "role_id"])
    op.create_table(
        "fnd_auth_group_roles",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "group_id", "role_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"], ["fnd_auth_groups.tenant_id", "fnd_auth_groups.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"], ["fnd_auth_roles.tenant_id", "fnd_auth_roles.id"]
        ),
    )
    op.create_index("idx_auth_group_roles_role", "fnd_auth_group_roles", ["tenant_id", "role_id"])
    op.create_table(
        "fnd_auth_group_provider_mappings",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_group_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(precision=53), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "provider", "provider_group_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "group_id"], ["fnd_auth_groups.tenant_id", "fnd_auth_groups.id"]
        ),
    )
    op.create_index(
        "idx_auth_group_provider_app_group", "fnd_auth_group_provider_mappings",
        ["tenant_id", "group_id"],
    )

    # Preserve currently effective direct role assignments. A role string
    # without a matching role catalog row stays in the legacy column for the
    # compatibility period rather than becoming an orphan foreign key.
    bind = op.get_bind()
    memberships = bind.execute(
        sa.text("SELECT user_id, org_id, roles FROM fnd_auth_memberships")
    ).mappings().all()
    now = time.time()
    for membership in memberships:
        try:
            role_names = json.loads(membership["roles"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(role_names, list):
            continue
        for role_name in set(str(name) for name in role_names if name):
            role_id = bind.execute(
                sa.text(
                    "SELECT id FROM fnd_auth_roles "
                    "WHERE tenant_id = :tenant_id AND name = :name"
                ),
                {"tenant_id": membership["org_id"], "name": role_name},
            ).scalar()
            if role_id:
                bind.execute(
                    sa.text(
                        "INSERT INTO fnd_auth_user_roles "
                        "(tenant_id, user_id, role_id, created_at) "
                        "VALUES (:tenant_id, :user_id, :role_id, :created_at)"
                    ),
                    {"tenant_id": membership["org_id"], "user_id": membership["user_id"],
                     "role_id": role_id, "created_at": now},
                )

    # Route ownership is deterministic for known existing page keys. Unknown
    # keys stay in FND and are reported by the app manifest reconciliation.
    for app_key, keys in {
        "aie": ("chat", "sessions", "analytics", "tokenomics.report", "tokenomics.estimation"),
        "wf": ("flow.instances", "flow.tasks", "flow.definitions", "flow.designer",
               "flow.deployments", "flow.events"),
    }.items():
        for page_key in keys:
            bind.execute(
                sa.text("UPDATE fnd_nav_pages SET app_key = :app_key WHERE key = :page_key"),
                {"app_key": app_key, "page_key": page_key},
            )


def downgrade() -> None:
    # Role assignment data is retained in the legacy JSON during the
    # compatibility period. New assignments cannot safely be collapsed into
    # that column, so a downgrade intentionally refuses to discard them.
    bind = op.get_bind()
    assigned = bind.execute(sa.text(
        "SELECT 1 FROM fnd_auth_user_roles LIMIT 1"
    )).first()
    if assigned:
        raise RuntimeError("Cannot downgrade: normalized user role assignments exist")
    assigned = bind.execute(sa.text(
        "SELECT 1 FROM fnd_auth_group_roles LIMIT 1"
    )).first()
    if assigned:
        raise RuntimeError("Cannot downgrade: group role assignments exist")

    op.drop_index("idx_auth_group_provider_app_group", table_name="fnd_auth_group_provider_mappings")
    op.drop_table("fnd_auth_group_provider_mappings")
    op.drop_index("idx_auth_group_roles_role", table_name="fnd_auth_group_roles")
    op.drop_table("fnd_auth_group_roles")
    op.drop_index("idx_auth_user_roles_role", table_name="fnd_auth_user_roles")
    op.drop_table("fnd_auth_user_roles")
    op.drop_index("idx_nav_page_aliases_page", table_name="fnd_nav_page_aliases")
    op.drop_table("fnd_nav_page_aliases")
    op.drop_index("idx_nav_pages_app", table_name="fnd_nav_pages")
    op.drop_column("fnd_nav_pages", "app_key")
    op.drop_constraint("uq_auth_roles_tenant_id", "fnd_auth_roles", type_="unique")
