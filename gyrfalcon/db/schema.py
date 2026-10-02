"""SQLAlchemy Core table metadata shared by the application and Alembic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column as SAColumn, Float, ForeignKeyConstraint,
    Index as SAIndex, MetaData, PrimaryKeyConstraint, SmallInteger, Table as SATable,
    Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()


def Column(name: str, type_name: str, constraints: str = "") -> SAColumn:
    """Compact PostgreSQL column declaration used below to keep the schema legible."""
    types = {"TEXT": Text, "INTEGER": BigInteger, "REAL": lambda: Float(precision=53), "BOOL": SmallInteger, "JSONB": JSONB}
    if type_name not in types:
        raise ValueError(f"Unsupported column type {type_name!r}")
    parts = constraints.upper()
    primary_key = "PRIMARY KEY" in parts
    unique = "UNIQUE" in parts
    nullable = "NOT NULL" not in parts and not primary_key
    default_match = re.search(r"\bDEFAULT\s+(.+)$", constraints, re.IGNORECASE)
    options = {"primary_key": primary_key, "unique": unique, "nullable": nullable}
    if default_match:
        options["server_default"] = text(default_match.group(1))
    return SAColumn(name, types[type_name](), **options)


@dataclass(frozen=True)
class Index:
    name: str
    table: str
    expr: str
    unique: bool = False


def _constraint(value: str):
    normalized = " ".join(value.split())
    match = re.fullmatch(r"PRIMARY KEY \(([^)]+)\)", normalized, re.IGNORECASE)
    if match:
        return PrimaryKeyConstraint(*(name.strip() for name in match.group(1).split(",")))
    match = re.fullmatch(r"UNIQUE \(([^)]+)\)", normalized, re.IGNORECASE)
    if match:
        return UniqueConstraint(*(name.strip() for name in match.group(1).split(",")))
    match = re.fullmatch(r"FOREIGN KEY \(([^)]+)\) REFERENCES ([^( ]+)\(([^)]+)\)", normalized, re.IGNORECASE)
    if match:
        local = [name.strip() for name in match.group(1).split(",")]
        remote = [f"{match.group(2)}.{name.strip()}" for name in match.group(3).split(",")]
        return ForeignKeyConstraint(local, remote)
    match = re.fullmatch(r"CHECK \((.*)\)", normalized, re.IGNORECASE)
    if match:
        return CheckConstraint(match.group(1))
    raise ValueError(f"Unsupported table constraint {value!r}")


def Table(name: str, columns: Sequence[SAColumn], table_constraints: Sequence[str] = (), indexes: Sequence[Index] = ()) -> SATable:
    table = SATable(name, metadata, *columns, *(_constraint(item) for item in table_constraints))
    for index in indexes:
        expressions = []
        for item in index.expr.split(","):
            token = item.strip().split()
            column = table.c[token[0]]
            expressions.append(column.desc() if len(token) > 1 and token[1].upper() == "DESC" else column)
        SAIndex(index.name, *expressions, unique=index.unique)
    return table


# --------------------------------------------------------------------------
# Visual flow definitions and execution.
FLOW_DT_DEFINITIONS = Table(
    name="fnd_flow_dt_definitions",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        SAColumn("enabled", Boolean, nullable=False, server_default=text("true")),
        SAColumn("published", Boolean, nullable=False, server_default=text("false")),
        Column("draft", "JSONB", "NOT NULL"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    table_constraints=("UNIQUE (tenant_id, id)", "UNIQUE (tenant_id, name)"),
)

FLOW_DT_DEFINITION_VERSIONS = Table(
    name="fnd_flow_dt_definition_versions",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("definition_id", "TEXT", "NOT NULL"),
        Column("version", "INTEGER", "NOT NULL"),
        Column("graph", "JSONB", "NOT NULL"),
        Column("attribute_snapshot", "JSONB", "NOT NULL"),
        Column("notification_snapshot", "JSONB", "NOT NULL"),
        Column("published_at", "REAL", "NOT NULL"),
        Column("published_by", "TEXT", "NOT NULL"),
    ),
    table_constraints=(
        "PRIMARY KEY (tenant_id, definition_id, version)",
        "FOREIGN KEY (tenant_id, definition_id) REFERENCES fnd_flow_dt_definitions(tenant_id, id)",
    ),
)

FLOW_DT_ATTRS = Table(
    name="fnd_flow_dt_attrs",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("definition_id", "TEXT", "NOT NULL"),
        Column("scope_path", "TEXT", "NOT NULL"),
        Column("attribute_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        Column("value_type", "TEXT", "NOT NULL"),
        Column("default_value", "JSONB"),
        Column("candidates", "JSONB"),
    ),
    table_constraints=(
        "PRIMARY KEY (tenant_id, definition_id, scope_path, attribute_id)",
        "UNIQUE (tenant_id, definition_id, scope_path, name)",
        "FOREIGN KEY (tenant_id, definition_id) REFERENCES fnd_flow_dt_definitions(tenant_id, id)",
    ),
)

FLOW_DT_NOTIF = Table(
    name="fnd_flow_dt_notif",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        Column("channel", "TEXT", "NOT NULL"),
        Column("recipient_kind", "TEXT", "NOT NULL"),
        Column("recipient_ref", "TEXT", "NOT NULL"),
        Column("agent_id", "TEXT"),
        Column("subject_template", "TEXT"),
        Column("body_template", "TEXT"),
        Column("config", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    table_constraints=("UNIQUE (tenant_id, id)", "UNIQUE (tenant_id, name)"),
)

FLOW_DT_DEPLOYMENTS = Table(
    name="fnd_flow_dt_deployments",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        Column("short_name", "TEXT", "NOT NULL"),
        Column("definition_id", "TEXT", "NOT NULL"),
        Column("version", "INTEGER", "NOT NULL"),
        Column("schedule", "TEXT"),
        Column("input_schema", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("parameters", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("allowed_service_account_ids", "JSONB", "NOT NULL DEFAULT '[]'::jsonb"),
        Column("paused", "BOOL", "NOT NULL DEFAULT 0"),
        Column("next_run_at", "REAL"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "UNIQUE (tenant_id, id)", "UNIQUE (tenant_id, name)",
        "UNIQUE (tenant_id, short_name)",
        "CHECK (short_name ~ '^[a-z0-9][a-z0-9_-]*$')",
        "FOREIGN KEY (tenant_id, definition_id, version) REFERENCES fnd_flow_dt_definition_versions(tenant_id, definition_id, version)",
    ),
    indexes=(Index("idx_flow_dt_deployments_schedule", "fnd_flow_dt_deployments", "paused, next_run_at"),),
)

FLOW_RT_STATUSES = Table(
    name="fnd_flow_rt_statuses",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("definition_id", "TEXT", "NOT NULL"),
        Column("version", "INTEGER", "NOT NULL"),
        Column("deployment_id", "TEXT"),
        Column("trigger_kind", "TEXT", "NOT NULL"),
        Column("trigger_ref", "TEXT"),
        Column("caller_key", "TEXT"),
        Column("input_hash", "TEXT"),
        Column("parameters", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("state", "TEXT", "NOT NULL"),
        Column("context", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("loop_state", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("current_node_path", "TEXT"),
        Column("result", "JSONB"),
        Column("error", "JSONB"),
        Column("lease_owner", "TEXT"),
        Column("lease_until", "REAL"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("started_at", "REAL"),
        Column("updated_at", "REAL", "NOT NULL"),
        Column("finished_at", "REAL"),
    ),
    table_constraints=(
        "UNIQUE (tenant_id, environment_id, id)",
        "UNIQUE (tenant_id, caller_key)",
        "FOREIGN KEY (tenant_id, definition_id, version) REFERENCES fnd_flow_dt_definition_versions(tenant_id, definition_id, version)",
        "FOREIGN KEY (tenant_id, deployment_id) REFERENCES fnd_flow_dt_deployments(tenant_id, id)",
    ),
    indexes=(
        Index("idx_flow_rt_statuses_state", "fnd_flow_rt_statuses", "tenant_id, environment_id, state, created_at DESC"),
        Index("idx_flow_rt_statuses_lease", "fnd_flow_rt_statuses", "state, lease_until"),
        Index("idx_flow_rt_statuses_definition", "fnd_flow_rt_statuses", "tenant_id, definition_id, created_at DESC"),
    ),
)

FLOW_RT_NODE_STATUSES = Table(
    name="fnd_flow_rt_node_statuses",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("run_id", "TEXT", "NOT NULL"),
        Column("node_path", "TEXT", "NOT NULL"),
        Column("node_id", "TEXT", "NOT NULL"),
        Column("node_kind", "TEXT", "NOT NULL"),
        Column("visit_seq", "INTEGER", "NOT NULL"),
        Column("attempt", "INTEGER", "NOT NULL DEFAULT 1"),
        Column("state", "TEXT", "NOT NULL"),
        Column("input_value", "JSONB"),
        Column("output_value", "JSONB"),
        Column("transient", "TEXT"),
        Column("error", "JSONB"),
        Column("wait_deadline", "REAL"),
        Column("timeout_transient", "TEXT"),
        Column("response_value", "JSONB"),
        Column("response_user_id", "TEXT"),
        Column("ai_session_id", "TEXT"),
        Column("started_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
        Column("finished_at", "REAL"),
    ),
    table_constraints=(
        "UNIQUE (tenant_id, environment_id, id)",
        "UNIQUE (tenant_id, environment_id, run_id, visit_seq)",
        "FOREIGN KEY (tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses(tenant_id, environment_id, id)",
        "FOREIGN KEY (tenant_id, environment_id, ai_session_id) REFERENCES ai_sessions(tenant_id, environment_id, id)",
    ),
    indexes=(
        Index("idx_flow_rt_nodes_path", "fnd_flow_rt_node_statuses", "tenant_id, environment_id, run_id, node_path, visit_seq DESC"),
        Index("idx_flow_rt_nodes_state", "fnd_flow_rt_node_statuses", "tenant_id, environment_id, state, updated_at DESC"),
    ),
)

FLOW_RT_EVENTS = Table(
    name="fnd_flow_rt_events",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("run_id", "TEXT", "NOT NULL"),
        Column("visit_id", "TEXT"),
        Column("event_type", "TEXT", "NOT NULL"),
        Column("payload", "JSONB", "NOT NULL DEFAULT '{}'::jsonb"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "FOREIGN KEY (tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses(tenant_id, environment_id, id)",
        "FOREIGN KEY (tenant_id, environment_id, visit_id) REFERENCES fnd_flow_rt_node_statuses(tenant_id, environment_id, id)",
    ),
    indexes=(Index("idx_flow_rt_events_run", "fnd_flow_rt_events", "tenant_id, environment_id, run_id, created_at"),),
)

FLOW_RT_ATTRS = Table(
    name="fnd_flow_rt_attrs",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("run_id", "TEXT", "NOT NULL"),
        Column("scope_path", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        Column("value", "JSONB"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "PRIMARY KEY (tenant_id, environment_id, run_id, scope_path, name)",
        "FOREIGN KEY (tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses(tenant_id, environment_id, id)",
    ),
)

FLOW_RT_NOTIF = Table(
    name="fnd_flow_rt_notif",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("run_id", "TEXT", "NOT NULL"),
        Column("visit_id", "TEXT", "NOT NULL"),
        Column("message_id", "TEXT", "NOT NULL"),
        Column("recipient_kind", "TEXT", "NOT NULL"),
        Column("recipient_ref", "TEXT", "NOT NULL"),
        Column("channel", "TEXT", "NOT NULL"),
        Column("attempt", "INTEGER", "NOT NULL DEFAULT 1"),
        Column("state", "TEXT", "NOT NULL"),
        Column("provider_id", "TEXT"),
        Column("error", "JSONB"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "FOREIGN KEY (tenant_id, environment_id, run_id) REFERENCES fnd_flow_rt_statuses(tenant_id, environment_id, id)",
        "FOREIGN KEY (tenant_id, environment_id, visit_id) REFERENCES fnd_flow_rt_node_statuses(tenant_id, environment_id, id)",
        "FOREIGN KEY (tenant_id, environment_id, message_id) REFERENCES ai_session_messages(tenant_id, environment_id, id)",
        "UNIQUE (tenant_id, environment_id, visit_id, message_id, recipient_kind, recipient_ref, channel, attempt)",
    ),
    indexes=(Index("idx_flow_rt_notif_visit", "fnd_flow_rt_notif", "tenant_id, environment_id, visit_id, state"),),
)

FLOW_DT_TABLES = (FLOW_DT_DEFINITIONS, FLOW_DT_DEFINITION_VERSIONS, FLOW_DT_ATTRS,
                  FLOW_DT_NOTIF, FLOW_DT_DEPLOYMENTS)
FLOW_RT_TABLES = (FLOW_RT_STATUSES, FLOW_RT_NODE_STATUSES, FLOW_RT_EVENTS,
                  FLOW_RT_ATTRS, FLOW_RT_NOTIF)


# --------------------------------------------------------------------------
# identity (§17.11 step 8)
#
# These are not flow tables, and this module's name is now a little narrow for
# what it holds. They live here anyway because they share one database and one
# Alembic revision history: splitting them out
# would mean two version counters for one physical schema, which is how a
# half-migrated database happens. Renaming the package is the tidier fix and
# is not worth the churn today.
# --------------------------------------------------------------------------

#: A customer. Gyrfalcon owns this concept rather than inheriting the IdP's
#: tenant, so one deployment can serve several organizations and an org can
#: outlive a change of identity provider.
AUTH_ORGS = Table(
    name="fnd_auth_orgs",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("name", "TEXT", "NOT NULL UNIQUE"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("disabled", "BOOL", "NOT NULL DEFAULT 0"),
    ),
)

#: A person. `subject` + `issuer` is the IdP's identity for them; `id` is
#: ours and never changes, so rows stay attributed across an email change or
#: a move to a different provider.
AUTH_USERS = Table(
    name="fnd_auth_users",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("issuer", "TEXT", "NOT NULL"),
        Column("subject", "TEXT", "NOT NULL"),
        Column("email", "TEXT"),
        Column("display_name", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("last_login_at", "REAL"),
        Column("disabled", "BOOL", "NOT NULL DEFAULT 0"),
        # 'human' | 'service'. A service account is an ordinary user row with
        # credentials and a membership, so it flows through role resolution and
        # grant enforcement identically — rather than resolving to an anonymous
        # shared principal that no audit trail can name.
        Column("kind", "TEXT", "NOT NULL DEFAULT 'human'"),
    ),
    table_constraints=("UNIQUE (issuer, subject)",),
    indexes=(Index("idx_users_email", "fnd_auth_users", "email"),),
)

#: Which orgs a person belongs to, and what they may do in each. Roles are
#: per-membership, not per-user: being an operator at one customer must not
#: make you one everywhere.
AUTH_MEMBERSHIPS = Table(
    name="fnd_auth_memberships",
    columns=(
        Column("user_id", "TEXT", "NOT NULL"),
        Column("org_id", "TEXT", "NOT NULL"),
        Column("roles", "TEXT", "NOT NULL DEFAULT '[]'"),
        Column("created_at", "REAL", "NOT NULL"),
        # The role this person last selected. Server-side rather than in the
        # browser so the choice survives a new device and is available when
        # the server builds that person's menu.
        Column("active_role", "TEXT"),
    ),
    table_constraints=("PRIMARY KEY (user_id, org_id)",),
    indexes=(Index("idx_memberships_org", "fnd_auth_memberships", "org_id"),),
)

# Optional reporting dimensions, scoped to a user's organization membership.
# Kept separate from auth_memberships so profile enrichment does not alter the
# identity/authorization model or older migration shapes.
AUTH_MEMBERSHIP_ATTRIBUTES = Table(
    name="fnd_auth_membership_attributes",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("department", "TEXT"),
        Column("business_unit", "TEXT"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    table_constraints=("PRIMARY KEY (tenant_id, user_id)",),
    indexes=(Index("idx_membership_attributes_department", "fnd_auth_membership_attributes",
                   "tenant_id, department"),
             Index("idx_membership_attributes_business_unit", "fnd_auth_membership_attributes",
                   "tenant_id, business_unit")),
)

#: Per-user credentials for non-interactive callers (§17.3). Only the hash is
#: stored — a leaked database must not yield working keys — with a short
#: non-secret prefix kept so a key can be looked up without scanning.
AUTH_API_KEYS = Table(
    name="fnd_auth_api_keys",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("prefix", "TEXT", "NOT NULL"),
        Column("key_hash", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("org_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("last_used_at", "REAL"),
        Column("revoked_at", "REAL"),
        # Which hashing scheme `key_hash` was produced with. Both the API-key
        # path and the OAuth service-account path happen to use an unsalted
        # SHA-256 hex digest today, so migrated credentials verify without a
        # special case — the column exists so that changing the scheme later is
        # a migration rather than a flag day.
        Column("hash_algo", "TEXT", "NOT NULL DEFAULT 'v1'"),
        # OAuth client id, for credentials belonging to a service account.
        # NULL for ordinary per-user API keys, which are looked up by `prefix`.
        Column("client_id", "TEXT"),
    ),
    indexes=(
        Index("idx_api_keys_prefix", "fnd_auth_api_keys", "prefix"),
        Index("idx_api_keys_client", "fnd_auth_api_keys", "client_id"),
        Index("idx_api_keys_user", "fnd_auth_api_keys", "user_id"),
    ),
)

# Local dashboard passwords are isolated from external IdP identities. The
# plaintext password is never stored; `password_hash` uses PBKDF2-HMAC-SHA256.
AUTH_LOCAL_CREDENTIALS = Table(
    name="fnd_auth_local_credentials",
    columns=(
        Column("username", "TEXT", "PRIMARY KEY"),
        Column("user_id", "TEXT", "NOT NULL UNIQUE"),
        Column("password_hash", "TEXT", "NOT NULL"),
        Column("must_change", "BOOL", "NOT NULL DEFAULT 0"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
    indexes=(Index("idx_local_credentials_user", "fnd_auth_local_credentials", "user_id", unique=True),),
)

AUTH_GROUPS = Table(
    name="fnd_auth_groups",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        Column("description", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=("UNIQUE (tenant_id, id)", "UNIQUE (tenant_id, name)"),
    indexes=(Index("idx_auth_groups_tenant", "fnd_auth_groups", "tenant_id, name"),),
)

AUTH_GROUP_MEMBERSHIPS = Table(
    name="fnd_auth_group_memberships",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("group_id", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=("PRIMARY KEY (tenant_id, group_id, user_id)",),
    indexes=(Index("idx_auth_group_memberships_user", "fnd_auth_group_memberships", "tenant_id, user_id"),),
)

AUTH_TABLES = (AUTH_ORGS, AUTH_USERS, AUTH_MEMBERSHIPS, AUTH_API_KEYS)


# --------------------------------------------------------------------------
# navigation & access control (17-users-roles-menus.md)
#
# A Role owns one Menu; a Menu resolves recursively to a set of Functions;
# that set is both the sidebar and the external invoke permission set. These
# four tables are tenant *configuration* — they carry `tenant_id` and
# deliberately no `user_id` (see `Scope.tenant_wide`). `nav_pages` is the one
# exception and is global: a route either shipped in this build or it did not.
# --------------------------------------------------------------------------

#: One navigable route that exists in this build. Global, not per-tenant — a
#: tenant does not invent routes, it only grants or withholds them via a Menu.
#: Keeping this separate from `nav_functions` is what makes a page Function's
#: target checkable at write time, so a typo'd route is caught by the admin
#: saving it rather than by a user hitting a 404.
NAV_PAGES = Table(
    name="fnd_nav_pages",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("key", "TEXT", "NOT NULL UNIQUE"),    # "flow.instances"
        Column("route", "TEXT", "NOT NULL"),          # "/flows/instances"
        Column("label", "TEXT", "NOT NULL"),
        Column("icon", "TEXT"),                        # lucide-react component name
        Column("enabled", "BOOL", "NOT NULL DEFAULT 1"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
)

#: A thing a menu entry points at, and the unit of authorization for the
#: external `/v1` surface. The four invocable kinds (flow/agent/skill/mcp) are
#: exactly the REST gateway's four resource types; `page` is nav-only and
#: never authorizes an invoke.
NAV_FUNCTIONS = Table(
    name="fnd_nav_functions",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("key", "TEXT", "NOT NULL"),            # stable public handle
        Column("name", "TEXT", "NOT NULL"),
        Column("icon", "TEXT"),
        Column("kind", "TEXT", "NOT NULL"),           # page|code|skill|agent|mcp|flow
        Column("target", "TEXT", "NOT NULL"),         # meaning depends on kind
        Column("params", "TEXT"),                      # JSON; defaults for invocable kinds
        Column("enabled", "BOOL", "NOT NULL DEFAULT 1"),
        Column("active_from", "REAL"),
        Column("active_to", "REAL"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    table_constraints=("UNIQUE (tenant_id, key)",),
    indexes=(Index("idx_functions_tenant", "fnd_nav_functions", "tenant_id"),),
)

NAV_MENUS = Table(
    name="fnd_nav_menus",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("name", "TEXT", "NOT NULL"),
        Column("icon", "TEXT"),
        Column("enabled", "BOOL", "NOT NULL DEFAULT 1"),
        Column("active_from", "REAL"),                 # NULL = no lower bound
        Column("active_to", "REAL"),                   # NULL = no upper bound
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    indexes=(Index("idx_nav_menus_tenant", "fnd_nav_menus", "tenant_id"),),
)

#: A leaf (references a Function) or a branch (references another Menu, whole).
#: Exactly one of `function_id` / `ref_menu_id` is set — enforced in the store.
NAV_MENU_ITEMS = Table(
    name="fnd_nav_menu_items",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("menu_id", "TEXT", "NOT NULL"),         # the owning Menu
        Column("sort_order", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("function_id", "TEXT"),                  # leaf
        Column("ref_menu_id", "TEXT"),                  # branch
        Column("access", "TEXT", "NOT NULL DEFAULT 'write'"),   # 'read' | 'write'
        Column("label_override", "TEXT"),
        Column("icon_override", "TEXT"),
        Column("enabled", "BOOL", "NOT NULL DEFAULT 1"),
        Column("active_from", "REAL"),
        Column("active_to", "REAL"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    indexes=(
        Index("idx_menu_items_menu", "fnd_nav_menu_items", "menu_id"),
        Index("idx_menu_items_function", "fnd_nav_menu_items", "function_id"),
        Index("idx_menu_items_ref", "fnd_nav_menu_items", "ref_menu_id"),
    ),
)

#: A named bundle of access carrying exactly one Menu. `name` matches a string
#: a Principal already carries in `auth_memberships.roles`, which is what turns
#: that free-form string into a resolvable tree.
AUTH_ROLES = Table(
    name="fnd_auth_roles",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("name", "TEXT", "NOT NULL"),
        Column("label", "TEXT"),                        # shown in the role switcher
        Column("menu_id", "TEXT"),                       # NULL = grants no nav
        Column("enabled", "BOOL", "NOT NULL DEFAULT 1"),
        Column("active_from", "REAL"),
        Column("active_to", "REAL"),
        Column("description", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    table_constraints=("UNIQUE (tenant_id, name)",),
    indexes=(Index("idx_roles_tenant", "fnd_auth_roles", "tenant_id"),),
)

#: Bumped on every write to any nav_* / auth_roles row in a tenant, in the same
#: transaction as the write. The grant cache compares this integer instead of
#: re-walking a menu tree, so a revoked role takes effect on the next request
#: across every process rather than after a TTL.
NAV_VERSIONS = Table(
    name="fnd_nav_versions",
    columns=(
        Column("tenant_id", "TEXT", "PRIMARY KEY"),
        Column("version", "INTEGER", "NOT NULL DEFAULT 1"),
        Column("updated_at", "REAL", "NOT NULL"),
    ),
)

NAV_TABLES = (NAV_PAGES, NAV_FUNCTIONS, NAV_MENUS, NAV_MENU_ITEMS,
              AUTH_ROLES, NAV_VERSIONS)



# --------------------------------------------------------------------------
# sessions — conversations, their messages, and per-call token usage (§19.7)
# --------------------------------------------------------------------------
#
# Conversation data lives beside flow, navigation, and auth so it follows the
# same database versioning and tenant scoping rules.

SESSIONS = Table(
    name="ai_sessions",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("source", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("model", "TEXT"),
        Column("parent_session_id", "TEXT"),
        Column("title", "TEXT"),
        Column("system_prompt", "TEXT"),
        Column("started_at", "REAL", "NOT NULL"),
        Column("ended_at", "REAL"),
        Column("run_status", "TEXT", "NOT NULL DEFAULT 'created'"),
        Column("run_error", "TEXT"),
        Column("last_active", "REAL"),
        # Token accounting. `uncached_input_tokens` rather than `input_tokens`
        # because the three classes must not overlap: providers disagree about
        # whether the prompt figure already contains the cached tokens, and one
        # column that means different things per row cannot be summed (§19.6).
        Column("uncached_input_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cache_read_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cache_write_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("output_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("reasoning_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cost_usd", "REAL", "NOT NULL DEFAULT 0"),
        Column("user_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("owner_user_id", "TEXT"),
        Column("visibility", "TEXT", "NOT NULL DEFAULT 'private'"),
        Column("group_id", "TEXT"),
        Column("prompt_snapshot_id", "TEXT"),
        Column("next_seq", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("next_turn_seq", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("context_revision", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("active_run_id", "TEXT"),
        Column("lease_owner", "TEXT"),
        Column("lease_until", "REAL"),
        Column("lease_epoch", "INTEGER", "NOT NULL DEFAULT 0"),
        # Populated for an agent run started by a visual-flow Activity visit.
        # Foreign keys are added with the new flow runtime tables.
        Column("flow_run_status_id", "TEXT"),
        Column("flow_node_status_id", "TEXT"),
    ),
    indexes=(
        Index("idx_sessions_owner", "ai_sessions", "tenant_id, last_active DESC"),
        Index("idx_sessions_user", "ai_sessions", "user_id, last_active DESC"),
        Index("idx_sessions_parent", "ai_sessions", "parent_session_id"),
        Index("uq_sessions_scope_id", "ai_sessions", "tenant_id, environment_id, id", unique=True),
        Index("idx_sessions_flow_run", "ai_sessions", "tenant_id, environment_id, flow_run_status_id"),
        Index("uq_sessions_flow_node", "ai_sessions", "tenant_id, environment_id, flow_node_status_id", unique=True),
        Index("idx_sessions_run_status", "ai_sessions", "tenant_id, environment_id, run_status, last_active DESC"),
    ),
)

SESSION_MESSAGES = Table(
    name="ai_session_messages",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("session_id", "TEXT", "NOT NULL"),
        Column("seq", "INTEGER", "NOT NULL"),
        Column("role", "TEXT", "NOT NULL"),
        Column("content", "TEXT"),
        Column("tool_call_id", "TEXT"),
        Column("tool_calls", "TEXT"),
        Column("tool_name", "TEXT"),
        Column("reasoning", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("run_id", "TEXT"),
        Column("input_ordinal", "INTEGER"),
        # Flow agent turns and Notification content share the session history.
        # The referenced node-visit table is added by the visual-flow runtime.
        Column("flow_node_status_id", "TEXT"),
        Column("message_kind", "TEXT", "NOT NULL DEFAULT 'conversation'"),
        Column("channel", "TEXT"),
        Column("direction", "TEXT"),
        Column("subject", "TEXT"),
        Column("sender", "TEXT"),
        Column("recipients", "JSONB"),
        Column("body_html", "TEXT"),
        Column("external_message_id", "TEXT"),
        Column("in_reply_to", "TEXT"),
        Column("headers", "JSONB"),
        Column("attachment_refs", "JSONB"),
    ),
    indexes=(
        Index("idx_session_messages_session", "ai_session_messages",
              "session_id, seq"),
        Index("idx_session_messages_owner", "ai_session_messages",
              "tenant_id, created_at DESC"),
        Index("uq_session_messages_order", "ai_session_messages",
              "tenant_id, environment_id, session_id, seq", unique=True),
        Index("idx_session_messages_flow_node", "ai_session_messages",
              "tenant_id, environment_id, flow_node_status_id, seq"),
        Index("uq_session_messages_scope_id", "ai_session_messages",
              "tenant_id, environment_id, id", unique=True),
    ),
)

SESSION_USAGE = Table(
    name="ai_session_usage",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("session_id", "TEXT", "NOT NULL"),
        Column("seq", "INTEGER", "NOT NULL"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("provider", "TEXT"),
        Column("model", "TEXT"),
        Column("uncached_input_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cache_read_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cache_write_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cache_ttl", "TEXT"),
        Column("output_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("reasoning_tokens", "INTEGER", "NOT NULL DEFAULT 0"),
        Column("cost_usd", "REAL", "NOT NULL DEFAULT 0"),
        # Which price list produced `cost_usd`. Without it a later catalog
        # refresh silently rewrites history: the stored number would no longer
        # correspond to any price list anyone could look up (§19.7).
        Column("pricing_catalog_version", "TEXT"),
        Column("user_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("environment_id", "TEXT", "NOT NULL DEFAULT 'default'"),
        Column("run_id", "TEXT"),
    ),
    indexes=(
        Index("idx_session_usage_session", "ai_session_usage", "session_id, seq"),
        Index("idx_session_usage_owner", "ai_session_usage",
              "tenant_id, created_at DESC"),
        Index("idx_session_usage_model", "ai_session_usage", "model"),
        Index("uq_session_usage_order", "ai_session_usage",
              "tenant_id, environment_id, session_id, seq", unique=True),
    ),
)

SESSION_ROUTES = Table(
    name="ai_session_routes",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL"),
        Column("platform", "TEXT", "NOT NULL"),
        Column("connection_id", "TEXT", "NOT NULL"),
        Column("chat_id", "TEXT", "NOT NULL"),
        Column("thread_id", "TEXT", "NOT NULL DEFAULT ''"),
        Column("agent_id", "TEXT", "NOT NULL DEFAULT ''"),
        Column("session_id", "TEXT", "NOT NULL"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "UNIQUE (tenant_id, environment_id, platform, connection_id, "
        "chat_id, thread_id, agent_id)",
        "FOREIGN KEY (tenant_id, environment_id, session_id) "
        "REFERENCES ai_sessions(tenant_id, environment_id, id)",
    ),
    indexes=(
        Index("idx_session_routes_session", "ai_session_routes",
              "tenant_id, environment_id, session_id"),
    ),
)

SESSION_CONTEXTS = Table(
    name="ai_session_contexts",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL"),
        Column("session_id", "TEXT", "NOT NULL"),
        Column("context_revision", "INTEGER", "NOT NULL"),
        Column("first_seq", "INTEGER", "NOT NULL"),
        Column("last_seq", "INTEGER", "NOT NULL"),
        Column("summary", "TEXT", "NOT NULL"),
        Column("model", "TEXT"),
        Column("token_count", "INTEGER", "NOT NULL"),
        Column("source_digest", "TEXT", "NOT NULL"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "PRIMARY KEY (tenant_id, environment_id, session_id, context_revision)",
        "FOREIGN KEY (tenant_id, environment_id, session_id) "
        "REFERENCES ai_sessions(tenant_id, environment_id, id)",
        "CHECK (first_seq >= 0 AND last_seq >= first_seq)",
    ),
)

SESSION_PROMPT_SNAPSHOTS = Table(
    name="ai_session_prompt_snapshots",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL"),
        Column("session_id", "TEXT", "NOT NULL"),
        Column("system_prompt", "TEXT", "NOT NULL"),
        Column("references_json", "TEXT", "NOT NULL DEFAULT '{}'"),
        Column("definition_revision", "TEXT"),
        Column("digest", "TEXT", "NOT NULL"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "FOREIGN KEY (tenant_id, environment_id, session_id) "
        "REFERENCES ai_sessions(tenant_id, environment_id, id)",
    ),
    indexes=(
        Index("idx_prompt_snapshots_session", "ai_session_prompt_snapshots",
              "tenant_id, environment_id, session_id, created_at"),
    ),
)

SESSION_PARTICIPANTS = Table(
    name="ai_session_participants",
    columns=(
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL"),
        Column("session_id", "TEXT", "NOT NULL"),
        Column("user_id", "TEXT", "NOT NULL"),
        Column("access_level", "TEXT", "NOT NULL"),
        Column("expires_at", "REAL"),
    ),
    table_constraints=(
        "PRIMARY KEY (tenant_id, environment_id, session_id, user_id)",
        "FOREIGN KEY (tenant_id, environment_id, session_id) "
        "REFERENCES ai_sessions(tenant_id, environment_id, id)",
    ),
)

CONVERSATION_GROUPS = Table(
    name="ai_conversation_groups",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("tenant_id", "TEXT", "NOT NULL"),
        Column("environment_id", "TEXT", "NOT NULL"),
        Column("name", "TEXT", "NOT NULL"),
        Column("platform", "TEXT"),
        Column("external_id", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
    ),
    table_constraints=(
        "UNIQUE (tenant_id, environment_id, id)",
        "UNIQUE (tenant_id, environment_id, platform, external_id)",
    ),
    indexes=(Index("idx_conversation_groups_name", "ai_conversation_groups",
                   "tenant_id, environment_id, name"),),
)

AI_STATE_META = Table(
    name="ai_state_meta",
    columns=(
        Column("key", "TEXT", "PRIMARY KEY"),
        Column("value", "TEXT"),
    ),
)

SESSION_TABLES = (
    SESSIONS, SESSION_MESSAGES, SESSION_USAGE, SESSION_ROUTES,
    SESSION_CONTEXTS, SESSION_PROMPT_SNAPSHOTS, SESSION_PARTICIPANTS,
    CONVERSATION_GROUPS, AI_STATE_META,
)


MAIL_TABLES = (
    Table("fnd_emails", (
        Column("id", "TEXT", "PRIMARY KEY"), Column("account", "TEXT", "NOT NULL"),
        Column("folder", "TEXT", "NOT NULL"), Column("uid", "INTEGER"),
        Column("thread_id", "TEXT", "NOT NULL"), Column("in_reply_to", "TEXT"),
        Column("references_ids", "TEXT"), Column("from_addr", "TEXT"),
        Column("to_addr", "TEXT"), Column("subject", "TEXT"),
        Column("date_header", "TEXT"), Column("body_text", "TEXT"),
        Column("raw_headers", "TEXT"), Column("direction", "TEXT", "NOT NULL DEFAULT 'inbound'"),
        Column("created_at", "REAL", "NOT NULL"),
    )),
    Table("fnd_email_classifications", (
        Column("email_id", "TEXT", "NOT NULL"), Column("label", "TEXT", "NOT NULL"),
        Column("confidence", "REAL"), Column("source", "TEXT", "NOT NULL DEFAULT 'rule'"),
        Column("created_at", "REAL", "NOT NULL"),
    ), table_constraints=("PRIMARY KEY (email_id, label)",
                          "FOREIGN KEY (email_id) REFERENCES fnd_emails(id)")),
    Table("fnd_classification_flow_map", (
        Column("label", "TEXT", "PRIMARY KEY"), Column("flow_name", "TEXT", "NOT NULL"),
        Column("enabled", "BOOL", "NOT NULL DEFAULT 1"),
        Column("parameters", "TEXT", "NOT NULL DEFAULT '{}'"),
        Column("created_at", "REAL", "NOT NULL"), Column("updated_at", "REAL", "NOT NULL"),
    )),
    Table("fnd_email_dispatches", (
        Column("id", "TEXT", "PRIMARY KEY"), Column("email_id", "TEXT", "NOT NULL"),
        Column("label", "TEXT", "NOT NULL"), Column("flow_name", "TEXT", "NOT NULL"),
        Column("run_id", "TEXT"), Column("status", "TEXT", "NOT NULL"),
        Column("detail", "TEXT"), Column("created_at", "REAL", "NOT NULL"),
    )),
)

ALL_TABLES = (FLOW_DT_TABLES + FLOW_RT_TABLES + AUTH_TABLES
              + (AUTH_LOCAL_CREDENTIALS, AUTH_GROUPS, AUTH_GROUP_MEMBERSHIPS) + NAV_TABLES
              + (AUTH_MEMBERSHIP_ATTRIBUTES,) + SESSION_TABLES + MAIL_TABLES)

# Agent and Notification messages carry reciprocal, tenant-scoped links to
# their visual flow visit. These constraints are installed by revision 0004.
SESSIONS.append_constraint(ForeignKeyConstraint(
    ["tenant_id", "environment_id", "flow_run_status_id"],
    ["fnd_flow_rt_statuses.tenant_id", "fnd_flow_rt_statuses.environment_id", "fnd_flow_rt_statuses.id"],
    name="fk_ai_sessions_flow_run_status",
))
SESSIONS.append_constraint(ForeignKeyConstraint(
    ["tenant_id", "environment_id", "flow_node_status_id"],
    ["fnd_flow_rt_node_statuses.tenant_id", "fnd_flow_rt_node_statuses.environment_id", "fnd_flow_rt_node_statuses.id"],
    name="fk_ai_sessions_flow_node_status",
))
SESSION_MESSAGES.append_constraint(ForeignKeyConstraint(
    ["tenant_id", "environment_id", "flow_node_status_id"],
    ["fnd_flow_rt_node_statuses.tenant_id", "fnd_flow_rt_node_statuses.environment_id", "fnd_flow_rt_node_statuses.id"],
    name="fk_ai_session_messages_flow_node_status",
))
