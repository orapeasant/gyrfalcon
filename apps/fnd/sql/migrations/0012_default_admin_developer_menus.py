"""Give Administrator and Developer the same default menu."""

from __future__ import annotations

import json
import time
import uuid

from alembic import op
import sqlalchemy as sa


revision = "0012_default_roles"
down_revision = "0011_access_apps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tenants = bind.execute(sa.text(
        "SELECT DISTINCT tenant_id FROM fnd_auth_roles "
        "WHERE name IN ('system_admin', 'app_developer', 'operator')"
    )).scalars().all()
    for tenant_id in tenants:
        roles = bind.execute(sa.text(
            "SELECT id, name, menu_id FROM fnd_auth_roles "
            "WHERE tenant_id = :tenant_id AND name IN "
            "('system_admin', 'app_developer', 'operator')"
        ), {"tenant_id": tenant_id}).mappings().all()
        by_name = {row["name"]: row for row in roles}

        administrator = by_name.get("system_admin")
        developer = by_name.get("app_developer")
        legacy_operator = by_name.get("operator")
        if administrator is None and legacy_operator is not None:
            bind.execute(sa.text(
                "UPDATE fnd_auth_roles SET name = 'system_admin', label = 'Administrator' "
                "WHERE tenant_id = :tenant_id AND id = :role_id"
            ), {"tenant_id": tenant_id, "role_id": legacy_operator["id"]})
            administrator = {**legacy_operator, "name": "system_admin"}
            legacy_operator = None
        elif administrator is None and developer is not None:
            administrator_id = uuid.uuid4().hex
            bind.execute(sa.text(
                "INSERT INTO fnd_auth_roles "
                "(id, name, label, menu_id, enabled, created_at, tenant_id) "
                "VALUES (:id, 'system_admin', 'Administrator', :menu_id, 1, :created_at, :tenant_id)"
            ), {"id": administrator_id, "menu_id": developer["menu_id"],
                "created_at": time.time(), "tenant_id": tenant_id})
            administrator = {"id": administrator_id, "name": "system_admin",
                             "menu_id": developer["menu_id"]}
        if administrator is None:
            continue

        # Existing default installations have a Full Access root menu. Prefer
        # it so both roles keep all pages; if an operator customized the role
        # catalog, use the existing Administrator root as the shared baseline.
        shared_menu_id = (legacy_operator["menu_id"] if legacy_operator else None)
        shared_menu_id = shared_menu_id or administrator["menu_id"]
        shared_menu_id = shared_menu_id or (developer["menu_id"] if developer else None)
        bind.execute(sa.text(
            "UPDATE fnd_auth_roles SET label = 'Administrator', menu_id = :menu_id "
            "WHERE tenant_id = :tenant_id AND id = :role_id"
        ), {"tenant_id": tenant_id, "role_id": administrator["id"],
            "menu_id": shared_menu_id})
        if developer is not None:
            bind.execute(sa.text(
                "UPDATE fnd_auth_roles SET label = 'Developer', menu_id = :menu_id "
                "WHERE tenant_id = :tenant_id AND id = :role_id"
            ), {"tenant_id": tenant_id, "role_id": developer["id"],
                "menu_id": shared_menu_id})
        else:
            developer_id = uuid.uuid4().hex
            bind.execute(sa.text(
                "INSERT INTO fnd_auth_roles "
                "(id, name, label, menu_id, enabled, created_at, tenant_id) "
                "VALUES (:id, 'app_developer', 'Developer', :menu_id, 1, :created_at, :tenant_id)"
            ), {"id": developer_id, "menu_id": shared_menu_id,
                "created_at": time.time(), "tenant_id": tenant_id})

        if legacy_operator is not None:
            old_id = legacy_operator["id"]
            new_id = administrator["id"]
            for table, owner_column in (("fnd_auth_user_roles", "user_id"),
                                        ("fnd_auth_group_roles", "group_id")):
                assignments = bind.execute(sa.text(
                    f"SELECT {owner_column} FROM {table} "
                    "WHERE tenant_id = :tenant_id AND role_id = :old_id"
                ), {"tenant_id": tenant_id, "old_id": old_id}).scalars().all()
                for owner_id in assignments:
                    bind.execute(sa.text(
                        f"INSERT INTO {table} (tenant_id, {owner_column}, role_id, created_at) "
                        f"VALUES (:tenant_id, :owner_id, :new_id, extract(epoch from now())) "
                        f"ON CONFLICT (tenant_id, {owner_column}, role_id) DO NOTHING"
                    ), {"tenant_id": tenant_id, "owner_id": owner_id, "new_id": new_id})
                bind.execute(sa.text(
                    f"DELETE FROM {table} WHERE tenant_id = :tenant_id AND role_id = :old_id"
                ), {"tenant_id": tenant_id, "old_id": old_id})
            bind.execute(sa.text(
                "DELETE FROM fnd_auth_roles WHERE tenant_id = :tenant_id AND id = :role_id"
            ), {"tenant_id": tenant_id, "role_id": old_id})

    # Keep the internal `operator` marker for tenant-wide engine operations,
    # while adding the Administrator navigation role for existing operators.
    memberships = bind.execute(sa.text(
        "SELECT user_id, org_id, roles FROM fnd_auth_memberships"
    )).mappings().all()
    for membership in memberships:
        try:
            roles = json.loads(membership["roles"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(roles, list) or "operator" not in roles:
            continue
        if "system_admin" not in roles:
            roles.append("system_admin")
            bind.execute(sa.text(
                "UPDATE fnd_auth_memberships SET roles = :roles "
                "WHERE user_id = :user_id AND org_id = :org_id"
            ), {"roles": json.dumps(sorted(set(roles))),
                "user_id": membership["user_id"], "org_id": membership["org_id"]})

    bind.execute(sa.text(
        "UPDATE fnd_nav_versions SET version = version + 1, "
        "updated_at = extract(epoch from now())"
    ))


def downgrade() -> None:
    raise RuntimeError(
        "The Administrator/Developer menu consolidation is not reversible; "
        "restore a database backup to return to the prior role menus."
    )
