"""Move Applications under the Developer's Application Developer menu."""

from alembic import op
import sqlalchemy as sa


revision = "0016_group_applications"
down_revision = "0015_developer_role_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tenants = bind.execute(sa.text(
        "SELECT tenant_id, id, menu_id FROM fnd_auth_roles WHERE name = 'app_developer'"
    )).mappings().all()
    for role in tenants:
        if role["menu_id"] is None:
            continue
        submenu = bind.execute(sa.text(
            "SELECT m.id FROM fnd_nav_menu_items AS branch "
            "JOIN fnd_nav_menus AS m ON m.tenant_id = branch.tenant_id AND m.id = branch.ref_menu_id "
            "WHERE branch.tenant_id = :tenant_id AND branch.menu_id = :root_id "
            "AND m.name IN ('Application', 'Application Developer') "
            "ORDER BY CASE WHEN m.name = 'Application Developer' THEN 0 ELSE 1 END LIMIT 1"
        ), {"tenant_id": role["tenant_id"], "root_id": role["menu_id"]}).first()
        if submenu is None:
            continue

        now = bind.execute(sa.text("SELECT extract(epoch from now())")).scalar_one()
        bind.execute(sa.text(
            "UPDATE fnd_nav_menus SET name = 'Application Developer', updated_at = :now "
            "WHERE tenant_id = :tenant_id AND id = :menu_id"
        ), {"now": now, "tenant_id": role["tenant_id"], "menu_id": submenu.id})

        function = bind.execute(sa.text(
            "SELECT id FROM fnd_nav_functions WHERE tenant_id = :tenant_id "
            "AND (key = 'applications.manage' OR "
            "(kind = 'page' AND target = 'applications.manage')) "
            "ORDER BY CASE WHEN key = 'applications.manage' THEN 0 ELSE 1 END LIMIT 1"
        ), {"tenant_id": role["tenant_id"]}).first()
        if function is None:
            continue
        root_item = bind.execute(sa.text(
            "SELECT id FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id "
            "AND menu_id = :menu_id AND function_id = :function_id LIMIT 1"
        ), {"tenant_id": role["tenant_id"], "menu_id": role["menu_id"],
            "function_id": function.id}).first()
        child_item = bind.execute(sa.text(
            "SELECT id FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id "
            "AND menu_id = :menu_id AND function_id = :function_id LIMIT 1"
        ), {"tenant_id": role["tenant_id"], "menu_id": submenu.id,
            "function_id": function.id}).first()
        if root_item is not None and child_item is None:
            bind.execute(sa.text(
                "UPDATE fnd_nav_menu_items SET sort_order = sort_order + 1 "
                "WHERE tenant_id = :tenant_id AND menu_id = :menu_id"
            ), {"tenant_id": role["tenant_id"], "menu_id": submenu.id})
            bind.execute(sa.text(
                "UPDATE fnd_nav_menu_items SET menu_id = :submenu_id, sort_order = 0, "
                "label_override = 'Manage Applications' "
                "WHERE tenant_id = :tenant_id AND id = :item_id"
            ), {"submenu_id": submenu.id, "tenant_id": role["tenant_id"],
                "item_id": root_item.id})
        elif root_item is not None:
            bind.execute(sa.text(
                "DELETE FROM fnd_nav_menu_items WHERE tenant_id = :tenant_id AND id = :item_id"
            ), {"tenant_id": role["tenant_id"], "item_id": root_item.id})

        bind.execute(sa.text(
            "INSERT INTO fnd_nav_versions (tenant_id, version, updated_at) "
            "VALUES (:tenant_id, 1, :now) ON CONFLICT (tenant_id) DO UPDATE "
            "SET version = fnd_nav_versions.version + 1, updated_at = EXCLUDED.updated_at"
        ), {"tenant_id": role["tenant_id"], "now": now})


def downgrade() -> None:
    raise RuntimeError("Developer menu placement is operator-managed and is not reverted.")
