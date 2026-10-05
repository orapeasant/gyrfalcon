"""Add Function, Menu, and Menu Item management to the Developer menu."""

from __future__ import annotations

import time
import uuid

from alembic import op
import sqlalchemy as sa


revision = "0014_developer_navigation"
down_revision = "0013_drop_legacy_nav"
branch_labels = None
depends_on = None


PAGES = (
    ("developer.functions", "/developer/functions", "Functions", "Workflow"),
    ("developer.menus", "/developer/menus", "Menus", "PanelTop"),
    ("developer.menu_items", "/developer/menu-items", "Menu Items", "ListTree"),
)


def _id() -> str:
    return uuid.uuid4().hex


def _ensure_page(bind, key: str, route: str, label: str, icon: str, now: float) -> None:
    bind.execute(sa.text(
        "INSERT INTO fnd_nav_pages (id, key, route, app_key, label, icon, enabled, created_at) "
        "VALUES (:id, :key, :route, 'fnd', :label, :icon, 1, :created_at) "
        "ON CONFLICT (key) DO NOTHING"
    ), {"id": _id(), "key": key, "route": route, "label": label,
        "icon": icon, "created_at": now})


def _ensure_function(bind, tenant_id: str, key: str, label: str,
                     icon: str, now: float) -> str:
    row = bind.execute(sa.text(
        "SELECT id FROM fnd_nav_functions WHERE tenant_id = :tenant_id AND key = :key"
    ), {"tenant_id": tenant_id, "key": key}).first()
    if row:
        return row.id
    function_id = _id()
    bind.execute(sa.text(
        "INSERT INTO fnd_nav_functions "
        "(id, key, name, icon, kind, target, params, enabled, active_from, active_to, "
        "created_at, updated_at, tenant_id) "
        "VALUES (:id, :key, :name, :icon, 'page', :target, NULL, 1, NULL, NULL, "
        ":created_at, :updated_at, :tenant_id) "
        "ON CONFLICT (tenant_id, key) DO NOTHING"
    ), {"id": function_id, "key": key, "name": label, "icon": icon,
        "target": key, "created_at": now, "updated_at": now,
        "tenant_id": tenant_id})
    row = bind.execute(sa.text(
        "SELECT id FROM fnd_nav_functions WHERE tenant_id = :tenant_id AND key = :key"
    ), {"tenant_id": tenant_id, "key": key}).one()
    return row.id


def _developer_menu(bind, tenant_id: str, developer_role, administrator_role,
                    now: float) -> str | None:
    if developer_role is None or developer_role.menu_id is None:
        return None
    menu_id = developer_role.menu_id
    if administrator_role is not None and administrator_role.menu_id == menu_id:
        existing = bind.execute(sa.text(
            "SELECT id FROM fnd_nav_menus WHERE tenant_id = :tenant_id "
            "AND name = 'Developer Menu' ORDER BY id LIMIT 1"
        ), {"tenant_id": tenant_id}).first()
        if existing:
            menu_id = existing.id
        else:
            original = bind.execute(sa.text(
                "SELECT * FROM fnd_nav_menus WHERE tenant_id = :tenant_id AND id = :id"
            ), {"tenant_id": tenant_id, "id": menu_id}).mappings().one()
            menu_id = _id()
            bind.execute(sa.text(
                "INSERT INTO fnd_nav_menus "
                "(id, name, icon, enabled, active_from, active_to, created_at, updated_at, tenant_id) "
                "VALUES (:id, 'Developer Menu', :icon, :enabled, :active_from, :active_to, "
                ":created_at, :updated_at, :tenant_id)"
            ), {"id": menu_id, "icon": original["icon"], "enabled": original["enabled"],
                "active_from": original["active_from"], "active_to": original["active_to"],
                "created_at": now, "updated_at": now, "tenant_id": tenant_id})
            old_items = bind.execute(sa.text(
                "SELECT * FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id AND menu_id = :menu_id"
            ), {"tenant_id": tenant_id, "menu_id": developer_role.menu_id}).mappings().all()
            for item in old_items:
                bind.execute(sa.text(
                    "INSERT INTO fnd_nav_menu_items "
                    "(id, menu_id, sort_order, function_id, ref_menu_id, access, label_override, "
                    "icon_override, enabled, active_from, active_to, tenant_id) "
                    "VALUES (:id, :menu_id, :sort_order, :function_id, :ref_menu_id, :access, "
                    ":label_override, :icon_override, :enabled, :active_from, :active_to, :tenant_id)"
                ), {"id": _id(), "menu_id": menu_id, "sort_order": item["sort_order"],
                    "function_id": item["function_id"], "ref_menu_id": item["ref_menu_id"],
                    "access": item["access"], "label_override": item["label_override"],
                    "icon_override": item["icon_override"], "enabled": item["enabled"],
                    "active_from": item["active_from"], "active_to": item["active_to"],
                    "tenant_id": tenant_id})
        bind.execute(sa.text(
            "UPDATE fnd_auth_roles SET menu_id = :menu_id "
            "WHERE tenant_id = :tenant_id AND id = :role_id"
        ), {"menu_id": menu_id, "tenant_id": tenant_id,
            "role_id": developer_role.id})
    else:
        bind.execute(sa.text(
            "UPDATE fnd_nav_menus SET name = 'Developer Menu', updated_at = :now "
            "WHERE tenant_id = :tenant_id AND id = :menu_id AND name = 'Default Menu'"
        ), {"now": now, "tenant_id": tenant_id, "menu_id": menu_id})
    return menu_id


def upgrade() -> None:
    bind = op.get_bind()
    now = time.time()
    for key, route, label, icon in PAGES:
        _ensure_page(bind, key, route, label, icon, now)

    tenants = bind.execute(sa.text(
        "SELECT DISTINCT tenant_id FROM fnd_auth_roles "
        "WHERE name IN ('app_developer', 'system_admin')"
    )).scalars().all()
    for tenant_id in tenants:
        roles = bind.execute(sa.text(
            "SELECT id, name, menu_id FROM fnd_auth_roles WHERE tenant_id = :tenant_id "
            "AND name IN ('app_developer', 'system_admin')"
        ), {"tenant_id": tenant_id}).mappings().all()
        by_name = {row["name"]: row for row in roles}
        developer_menu_id = _developer_menu(
            bind, tenant_id, by_name.get("app_developer"),
            by_name.get("system_admin"), now,
        )
        if developer_menu_id is None:
            continue

        application_menu = bind.execute(sa.text(
            "SELECT id FROM fnd_nav_menus WHERE tenant_id = :tenant_id "
            "AND name = 'Application' ORDER BY id LIMIT 1"
        ), {"tenant_id": tenant_id}).first()
        if application_menu is None:
            application_menu_id = _id()
            bind.execute(sa.text(
                "INSERT INTO fnd_nav_menus "
                "(id, name, icon, enabled, created_at, updated_at, tenant_id) "
                "VALUES (:id, 'Application', 'PanelTop', 1, :now, :now, :tenant_id)"
            ), {"id": application_menu_id, "now": now, "tenant_id": tenant_id})
        else:
            application_menu_id = application_menu.id

        for key, _route, label, icon in PAGES:
            function_id = _ensure_function(bind, tenant_id, key, label, icon, now)
            exists = bind.execute(sa.text(
                "SELECT 1 FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id "
                "AND menu_id = :menu_id AND function_id = :function_id LIMIT 1"
            ), {"tenant_id": tenant_id, "menu_id": application_menu_id,
                "function_id": function_id}).first()
            if not exists:
                sort_order = bind.execute(sa.text(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM fnd_nav_menu_items "
                    "WHERE tenant_id = :tenant_id AND menu_id = :menu_id"
                ), {"tenant_id": tenant_id, "menu_id": application_menu_id}).scalar_one()
                bind.execute(sa.text(
                    "INSERT INTO fnd_nav_menu_items "
                    "(id, menu_id, sort_order, function_id, access, enabled, tenant_id) "
                    "VALUES (:id, :menu_id, :sort_order, :function_id, 'write', 1, :tenant_id)"
                ), {"id": _id(), "menu_id": application_menu_id,
                    "sort_order": sort_order, "function_id": function_id,
                    "tenant_id": tenant_id})

        branch_exists = bind.execute(sa.text(
            "SELECT 1 FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id "
            "AND menu_id = :menu_id AND ref_menu_id = :child_id LIMIT 1"
        ), {"tenant_id": tenant_id, "menu_id": developer_menu_id,
            "child_id": application_menu_id}).first()
        if not branch_exists:
            sort_order = bind.execute(sa.text(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM fnd_nav_menu_items "
                "WHERE tenant_id = :tenant_id AND menu_id = :menu_id"
            ), {"tenant_id": tenant_id, "menu_id": developer_menu_id}).scalar_one()
            bind.execute(sa.text(
                "INSERT INTO fnd_nav_menu_items "
                "(id, menu_id, sort_order, ref_menu_id, enabled, tenant_id) "
                "VALUES (:id, :menu_id, :sort_order, :child_id, 1, :tenant_id)"
            ), {"id": _id(), "menu_id": developer_menu_id,
                "sort_order": sort_order, "child_id": application_menu_id,
                "tenant_id": tenant_id})

        bind.execute(sa.text(
            "INSERT INTO fnd_nav_versions (tenant_id, version, updated_at) "
            "VALUES (:tenant_id, 1, :now) ON CONFLICT (tenant_id) DO UPDATE "
            "SET version = fnd_nav_versions.version + 1, updated_at = EXCLUDED.updated_at"
        ), {"tenant_id": tenant_id, "now": now})


def downgrade() -> None:
    raise RuntimeError(
        "Developer navigation pages and their menu configuration are not removed by downgrade."
    )
