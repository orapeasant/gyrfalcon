"""Separate user, group, and role page registrations in the Security menu."""

from __future__ import annotations

import time
import uuid

from alembic import op
import sqlalchemy as sa


revision = "0018_security_master_detail"
down_revision = "0017_application_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    now = time.time()
    page = bind.execute(sa.text(
        "SELECT id FROM fnd_nav_pages WHERE key = 'security.groups'"
    )).scalar()
    if page is None:
        page = uuid.uuid4().hex
        bind.execute(sa.text(
            "INSERT INTO fnd_nav_pages (id, key, route, app_key, label, icon, enabled, created_at) "
            "VALUES (:id, 'security.groups', '/security/groups', 'fnd', 'Groups', 'UsersRound', 1, :now)"
        ), {"id": page, "now": now})

    bind.execute(sa.text(
        "UPDATE fnd_nav_pages SET label = 'Users' WHERE key = 'security.users'"
    ))
    rows = bind.execute(sa.text(
        "SELECT tenant_id, id, application_id FROM fnd_nav_functions "
        "WHERE key = 'security.users'"
    )).all()
    for tenant_id, user_function_id, application_id in rows:
        bind.execute(sa.text(
            "UPDATE fnd_nav_functions SET name = 'Users', updated_at = :now "
            "WHERE tenant_id = :tenant_id AND id = :id"
        ), {"now": now, "tenant_id": tenant_id, "id": user_function_id})

        function_id = bind.execute(sa.text(
            "SELECT id FROM fnd_nav_functions WHERE tenant_id = :tenant_id AND key = 'security.groups'"
        ), {"tenant_id": tenant_id}).scalar()
        if function_id is None:
            function_id = uuid.uuid4().hex
            bind.execute(sa.text(
                "INSERT INTO fnd_nav_functions "
                "(id, key, name, icon, kind, target, params, enabled, active_from, active_to, "
                "created_at, updated_at, tenant_id, application_id) "
                "VALUES (:id, 'security.groups', 'Groups', 'UsersRound', 'page', 'security.groups', "
                "NULL, 1, NULL, NULL, :now, :now, :tenant_id, :application_id)"
            ), {"id": function_id, "now": now, "tenant_id": tenant_id,
                "application_id": application_id})

        menu = bind.execute(sa.text(
            "SELECT i.menu_id FROM fnd_nav_menu_items i "
            "JOIN fnd_nav_menus m ON m.id = i.menu_id AND m.tenant_id = i.tenant_id "
            "WHERE i.tenant_id = :tenant_id AND m.name = 'Security' "
            "AND i.function_id = :function_id LIMIT 1"
        ), {"tenant_id": tenant_id, "function_id": user_function_id}).scalar()
        if menu is None:
            continue
        exists = bind.execute(sa.text(
            "SELECT 1 FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id "
            "AND menu_id = :menu_id AND function_id = :function_id"
        ), {"tenant_id": tenant_id, "menu_id": menu, "function_id": function_id}).scalar()
        if exists is None:
            order = bind.execute(sa.text(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM fnd_nav_menu_items "
                "WHERE tenant_id = :tenant_id AND menu_id = :menu_id"
            ), {"tenant_id": tenant_id, "menu_id": menu}).scalar()
            bind.execute(sa.text(
                "INSERT INTO fnd_nav_menu_items "
                "(id, menu_id, sort_order, function_id, ref_menu_id, access, label_override, "
                "icon_override, enabled, active_from, active_to, tenant_id, application_id) "
                "VALUES (:id, :menu_id, :sort_order, :function_id, NULL, 'write', NULL, NULL, "
                "1, NULL, NULL, :tenant_id, :application_id)"
            ), {"id": uuid.uuid4().hex, "menu_id": menu, "sort_order": order,
                "function_id": function_id, "tenant_id": tenant_id,
                "application_id": application_id})

    bind.execute(sa.text(
        "UPDATE fnd_nav_versions SET version = version + 1, updated_at = :now"
    ), {"now": now})


def downgrade() -> None:
    raise RuntimeError(
        "The Security user/group page split is not automatically reversible; "
        "restore a database backup to return to the prior page layout."
    )
