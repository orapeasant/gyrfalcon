"""Small SQL helpers retained by non-flow feature stores.

Visual flow persistence uses SQLAlchemy Core tables and queries in
``flow/runtime_store.py``. Retired ``@flow`` run, event, and deployment SQL is
intentionally absent from this catalog.
"""

from __future__ import annotations

from typing import Sequence

from gyrfalcon.db.scope import Scope, merge


def compose_where(scope: "Scope", clauses: Sequence[str] = (),
                  table: str = "") -> tuple[str, tuple]:
    """The one place a WHERE clause is assembled (§17.5).

    Every read below routes through this, so "is this query tenant-scoped" is
    a property of one function rather than of thirty call sites.
    """
    return merge(scope, clauses, table)


def date_bucket(grain: str, expression: str) -> str:
    if grain not in {"day", "week", "month", "quarter"}:
        raise ValueError(f"unsupported date grain: {grain}")
    if grain == "day":
        return f"to_char(to_timestamp({expression}) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
    fmt = 'IYYY-"W"IW' if grain == "week" else "YYYY-MM" if grain == "month" else 'YYYY-"Q"Q'
    return f"to_char(date_trunc('{grain}', to_timestamp({expression}) AT TIME ZONE 'UTC'), '{fmt}')"


def placeholders(n: int) -> str:
    """`?,?,?` for an IN list of `n` bound values.

    StoreConnection binds these placeholders through SQLAlchemy named parameters.
    """
    return ",".join("?" * n)


def insert_ignore(table: str, columns: Sequence[str], conflict: str) -> str:
    cols = ", ".join(columns)
    vals = ",".join("?" * len(columns))
    return f"INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) DO NOTHING"


# ==========================================================================
# identity  (auth/store.py, §17.11 step 8)
#
# These are plain constants, not scope-taking builders, and that is not an
# oversight: they are read *during authentication*, before a principal exists,
# so there is nothing to scope by yet. Scoping the identity store by the
# identity it establishes would be circular. Everything here is reachable only
# from `auth/store.py`, which is the boundary that keeps it honest.
# ==========================================================================

INSERT_ORG = "INSERT INTO fnd_auth_orgs (id, name, created_at, disabled) VALUES (?,?,?,0)"
GET_ORG = "SELECT * FROM fnd_auth_orgs WHERE id = ?"
GET_ORG_BY_NAME = "SELECT * FROM fnd_auth_orgs WHERE name = ?"
LIST_ORGS = "SELECT * FROM fnd_auth_orgs ORDER BY name"

INSERT_USER = """
    INSERT INTO fnd_auth_users
        (id, issuer, subject, email, display_name, created_at, last_login_at, disabled)
    VALUES (?,?,?,?,?,?,?,0)
"""
GET_USER = "SELECT * FROM fnd_auth_users WHERE id = ?"
FIND_USER = "SELECT * FROM fnd_auth_users WHERE issuer = ? AND subject = ?"
TOUCH_USER = """
    UPDATE fnd_auth_users SET email = ?, display_name = ?, last_login_at = ?
     WHERE id = ?
"""
LIST_USERS = "SELECT * FROM fnd_auth_users ORDER BY created_at"
FIND_LOCAL_CREDENTIAL = "SELECT * FROM fnd_auth_local_credentials WHERE username = ?"
GET_LOCAL_CREDENTIAL_BY_USER = "SELECT * FROM fnd_auth_local_credentials WHERE user_id = ?"
HAS_LOCAL_CREDENTIALS = "SELECT username FROM fnd_auth_local_credentials LIMIT 1"
INSERT_LOCAL_CREDENTIAL = """
    INSERT INTO fnd_auth_local_credentials (username, user_id, password_hash, must_change, updated_at)
    VALUES (?,?,?,?,?)
"""
UPDATE_LOCAL_CREDENTIAL = """
    UPDATE fnd_auth_local_credentials SET password_hash = ?, must_change = 0, updated_at = ?
    WHERE user_id = ?
"""
RESET_LOCAL_CREDENTIAL = """
    UPDATE fnd_auth_local_credentials SET password_hash = ?, must_change = 1, updated_at = ?
    WHERE user_id = ?
"""
TOUCH_LOCAL_LOGIN = "UPDATE fnd_auth_users SET last_login_at = ? WHERE id = ?"
LIST_TENANT_USERS = """
    SELECT au.id, au.email, au.display_name, au.created_at, au.last_login_at,
           au.disabled, au.kind, am.roles, alc.username
      FROM fnd_auth_users au
      JOIN fnd_auth_memberships am ON am.user_id = au.id AND am.org_id = ?
      LEFT JOIN fnd_auth_local_credentials alc ON alc.user_id = au.id
     ORDER BY au.display_name, au.email, au.id
"""
GET_TENANT_USER = """
    SELECT au.id FROM fnd_auth_users au JOIN fnd_auth_memberships am ON am.user_id = au.id
     WHERE au.id = ? AND am.org_id = ?
"""
UPDATE_USER_PROFILE = "UPDATE fnd_auth_users SET display_name = ?, email = ?, disabled = ? WHERE id = ?"
LIST_AUTH_GROUPS = "SELECT * FROM fnd_auth_groups WHERE tenant_id = ? ORDER BY name"
GET_AUTH_GROUP = "SELECT * FROM fnd_auth_groups WHERE tenant_id = ? AND id = ?"
INSERT_AUTH_GROUP = "INSERT INTO fnd_auth_groups (id, tenant_id, name, description, created_at) VALUES (?,?,?,?,?)"
UPDATE_AUTH_GROUP = "UPDATE fnd_auth_groups SET name = ?, description = ? WHERE tenant_id = ? AND id = ?"
DELETE_AUTH_GROUP_MEMBERS = "DELETE FROM fnd_auth_group_memberships WHERE tenant_id = ? AND group_id = ?"
DELETE_AUTH_GROUP = "DELETE FROM fnd_auth_groups WHERE tenant_id = ? AND id = ?"
LIST_USER_GROUPS = "SELECT group_id FROM fnd_auth_group_memberships WHERE tenant_id = ? AND user_id = ?"
DELETE_USER_GROUPS = "DELETE FROM fnd_auth_group_memberships WHERE tenant_id = ? AND user_id = ?"
INSERT_GROUP_MEMBER = "INSERT INTO fnd_auth_group_memberships (tenant_id, group_id, user_id, created_at) VALUES (?,?,?,?)"
LIST_GROUP_MEMBERS = "SELECT user_id FROM fnd_auth_group_memberships WHERE tenant_id = ? AND group_id = ?"
UPDATE_USER_ROLES = "UPDATE fnd_auth_memberships SET roles = ? WHERE user_id = ? AND org_id = ?"

GET_MEMBERSHIP = "SELECT * FROM fnd_auth_memberships WHERE user_id = ? AND org_id = ?"
LIST_MEMBERSHIPS = "SELECT * FROM fnd_auth_memberships WHERE user_id = ? ORDER BY org_id"
LIST_ORG_MEMBERS = "SELECT * FROM fnd_auth_memberships WHERE org_id = ? ORDER BY user_id"
UPSERT_MEMBERSHIP_UPDATE = (
    "UPDATE fnd_auth_memberships SET roles = ? WHERE user_id = ? AND org_id = ?"
)
DELETE_MEMBERSHIP = "DELETE FROM fnd_auth_memberships WHERE user_id = ? AND org_id = ?"

MEMBERSHIP_COLUMNS = ("user_id", "org_id", "roles", "created_at")


def insert_membership(d: "Dialect") -> str:
    return d.insert_ignore("fnd_auth_memberships", MEMBERSHIP_COLUMNS,
                           conflict="user_id, org_id")


INSERT_API_KEY = """
    INSERT INTO fnd_auth_api_keys
        (id, prefix, key_hash, user_id, org_id, name, created_at)
    VALUES (?,?,?,?,?,?,?)
"""
#: Looked up by the non-secret prefix; the hash is then compared in constant
#: time. Finding the row must not itself be the authentication decision.
FIND_API_KEYS_BY_PREFIX = (
    "SELECT * FROM fnd_auth_api_keys WHERE prefix = ? AND revoked_at IS NULL"
)
TOUCH_API_KEY = "UPDATE fnd_auth_api_keys SET last_used_at = ? WHERE id = ?"
REVOKE_API_KEY = "UPDATE fnd_auth_api_keys SET revoked_at = ? WHERE id = ?"
LIST_API_KEYS = (
    "SELECT * FROM fnd_auth_api_keys WHERE user_id = ? ORDER BY created_at DESC"
)


# ==========================================================================
# navigation & access control  (17-users-roles-menus.md)
#
# `nav_pages` is global — a route either shipped in this build or it did not —
# so its reads are plain constants. The other four tables are tenant
# configuration and their reads are builders taking a `Scope`, which the
# mechanical test in `tests/flow/test_scope.py` enforces.
#
# Every builder here narrows with `scope.tenant_wide()`: these tables carry a
# `tenant_id` and deliberately no `user_id`, so the owner half of an ordinary
# scope would reference a column that does not exist. Applying it *here*
# rather than at each call site makes "which tables are tenant-wide" a
# property of the table.
# ==========================================================================

# -- pages (global) --------------------------------------------------------

PAGE_COLUMNS = ("id", "key", "route", "label", "icon", "enabled", "created_at")

INSERT_PAGE = """
    INSERT INTO fnd_nav_pages (id, key, route, label, icon, enabled, created_at)
    VALUES (?,?,?,?,?,?,?)
"""
GET_PAGE = "SELECT * FROM fnd_nav_pages WHERE id = ?"
GET_PAGE_BY_KEY = "SELECT * FROM fnd_nav_pages WHERE key = ?"
LIST_PAGES = "SELECT * FROM fnd_nav_pages ORDER BY key"
UPDATE_PAGE = (
    "UPDATE fnd_nav_pages SET route = ?, label = ?, icon = ?, enabled = ? WHERE id = ?"
)
DELETE_PAGE = "DELETE FROM fnd_nav_pages WHERE id = ?"


def insert_page(d: "Dialect") -> str:
    """Seeding re-runs on every start, so a page that already exists is a
    no-op rather than a duplicate-key failure."""
    return d.insert_ignore("fnd_nav_pages", PAGE_COLUMNS, conflict="key")


# -- functions -------------------------------------------------------------

FUNCTION_COLUMNS = (
    "id", "key", "name", "icon", "kind", "target", "params", "enabled",
    "active_from", "active_to", "created_at", "updated_at", "tenant_id",
)


def insert_function(d: "Dialect") -> str:
    return d.insert_ignore("fnd_nav_functions", FUNCTION_COLUMNS,
                           conflict="tenant_id, key")


def get_function(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM fnd_nav_functions {where}", params


def get_function_by_key(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["key = ?"])
    return f"SELECT * FROM fnd_nav_functions {where}", params


def list_functions(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide())
    return f"SELECT * FROM fnd_nav_functions {where} ORDER BY name", params


def update_function(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE fnd_nav_functions SET name = ?, icon = ?, kind = ?, target = ?, "
        f"params = ?, enabled = ?, active_from = ?, active_to = ?, updated_at = ? {where}",
        params,
    )


def delete_function(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM fnd_nav_functions {where}", params


# -- menus -----------------------------------------------------------------

MENU_COLUMNS = ("id", "name", "icon", "enabled", "active_from", "active_to",
                "created_at", "updated_at", "tenant_id")


def insert_menu(d: "Dialect") -> str:
    return d.insert_ignore("fnd_nav_menus", MENU_COLUMNS, conflict="id")


def get_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM fnd_nav_menus {where}", params


def list_menus(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide())
    return f"SELECT * FROM fnd_nav_menus {where} ORDER BY name", params


def update_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE fnd_nav_menus SET name = ?, icon = ?, enabled = ?, "
        f"active_from = ?, active_to = ?, updated_at = ? {where}",
        params,
    )


def delete_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM fnd_nav_menus {where}", params


# -- menu items ------------------------------------------------------------

MENU_ITEM_COLUMNS = (
    "id", "menu_id", "sort_order", "function_id", "ref_menu_id", "access",
    "label_override", "icon_override", "enabled", "active_from", "active_to",
    "tenant_id",
)


def insert_menu_item(d: "Dialect") -> str:
    return d.insert_ignore("fnd_nav_menu_items", MENU_ITEM_COLUMNS, conflict="id")


def get_menu_item(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM fnd_nav_menu_items {where}", params


def list_menu_items(scope: "Scope") -> tuple[str, tuple]:
    """One menu's items, in render order. `sort_order` first, `id` as the
    tiebreak so two items sharing an order still come back deterministically
    rather than in whatever order the backend happens to return."""
    where, params = compose_where(scope.tenant_wide(), ["menu_id = ?"])
    return f"SELECT * FROM fnd_nav_menu_items {where} ORDER BY sort_order, id", params


def items_referencing_menu(scope: "Scope") -> tuple[str, tuple]:
    """Branches pointing at a given menu — for cycle detection and for
    reporting what would break before a menu is deleted."""
    where, params = compose_where(scope.tenant_wide(), ["ref_menu_id = ?"])
    return f"SELECT * FROM fnd_nav_menu_items {where}", params


def update_menu_item(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE fnd_nav_menu_items SET sort_order = ?, access = ?, "
        "label_override = ?, icon_override = ?, enabled = ?, "
        f"active_from = ?, active_to = ? {where}",
        params,
    )


def set_menu_item_order(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"UPDATE fnd_nav_menu_items SET sort_order = ? {where}", params


def delete_menu_item(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM fnd_nav_menu_items {where}", params


def delete_items_of_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["menu_id = ?"])
    return f"DELETE FROM fnd_nav_menu_items {where}", params


# -- roles -----------------------------------------------------------------

ROLE_COLUMNS = ("id", "name", "label", "menu_id", "enabled", "active_from",
                "active_to", "description", "created_at", "tenant_id")


def insert_role(d: "Dialect") -> str:
    return d.insert_ignore("fnd_auth_roles", ROLE_COLUMNS, conflict="tenant_id, name")


def get_role(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM fnd_auth_roles {where}", params


def get_role_by_name(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["name = ?"])
    return f"SELECT * FROM fnd_auth_roles {where}", params


def list_roles(scope: "Scope") -> tuple[str, tuple]:
    """Ordered by `label` so §4.1's "first live nav-granting role" fallback is
    deterministic — two logins must never disagree about which role is
    active."""
    where, params = compose_where(scope.tenant_wide())
    return f"SELECT * FROM fnd_auth_roles {where} ORDER BY label, name", params


def update_role(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE fnd_auth_roles SET name = ?, label = ?, menu_id = ?, enabled = ?, "
        f"active_from = ?, active_to = ?, description = ? {where}",
        params,
    )


def delete_role(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM fnd_auth_roles {where}", params


# -- the version counter ---------------------------------------------------
#
# Keyed by tenant and bumped inside the same transaction as any nav write, so
# every process invalidates its grant cache on the next request rather than
# after a TTL. Not a scoped builder: the tenant is the primary key, and the
# cache asks about exactly one.

GET_NAV_VERSION = "SELECT version FROM fnd_nav_versions WHERE tenant_id = ?"
BUMP_NAV_VERSION = (
    "UPDATE fnd_nav_versions SET version = version + 1, updated_at = ? "
    "WHERE tenant_id = ?"
)
NAV_VERSION_COLUMNS = ("tenant_id", "version", "updated_at")


def insert_nav_version(d: "Dialect") -> str:
    return d.insert_ignore("fnd_nav_versions", NAV_VERSION_COLUMNS, conflict="tenant_id")


# -- service accounts on auth_api_keys -------------------------------------

FIND_API_KEY_BY_CLIENT_ID = (
    "SELECT * FROM fnd_auth_api_keys WHERE client_id = ? AND revoked_at IS NULL"
)


# --------------------------------------------------------------------------
# sessions, messages and per-call usage (§19.7)
# --------------------------------------------------------------------------

SESSION_COLUMNS = (
    "id", "source", "agent_id", "model", "parent_session_id", "title",
    "system_prompt", "started_at", "ended_at", "last_active",
    "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
    "output_tokens", "reasoning_tokens", "cost_usd", "user_id", "tenant_id",
    "owner_user_id",
)

SESSION_MESSAGE_COLUMNS = (
    "id", "session_id", "seq", "role", "content", "tool_call_id", "tool_calls",
    "tool_name", "reasoning", "created_at", "user_id", "tenant_id",
)

SESSION_USAGE_COLUMNS = (
    "id", "session_id", "seq", "created_at", "provider", "model",
    "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
    "cache_ttl", "output_tokens", "reasoning_tokens", "cost_usd",
    "pricing_catalog_version", "user_id", "tenant_id",
)


def insert_session() -> str:
    return (f"INSERT INTO ai_sessions ({', '.join(SESSION_COLUMNS)}) "
            f"VALUES ({placeholders(len(SESSION_COLUMNS))})")


def insert_session_message() -> str:
    return (f"INSERT INTO ai_session_messages ({', '.join(SESSION_MESSAGE_COLUMNS)}) "
            f"VALUES ({placeholders(len(SESSION_MESSAGE_COLUMNS))})")


def insert_session_usage() -> str:
    return (f"INSERT INTO ai_session_usage ({', '.join(SESSION_USAGE_COLUMNS)}) "
            f"VALUES ({placeholders(len(SESSION_USAGE_COLUMNS))})")


def get_session(scope: "Scope") -> tuple[str, tuple]:
    """One session by id — scoped, because "fetch by primary key" is exactly
    where a forgotten tenant filter hides."""
    where, params = compose_where(scope, ["id = ?"], table="ai_sessions")
    return f"SELECT * FROM ai_sessions {where}", params


def list_sessions(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, table="ai_sessions")
    return (f"SELECT * FROM ai_sessions {where} "
            f"ORDER BY last_active DESC LIMIT ? OFFSET ?"), params


def list_sessions_by_source(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["source = ?"], table="ai_sessions")
    return f"SELECT * FROM ai_sessions {where} ORDER BY last_active DESC LIMIT ? OFFSET ?", params


def count_sessions(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, table="ai_sessions")
    return f"SELECT COUNT(*) AS count FROM ai_sessions {where}", params


GET_STATE_META = "SELECT value FROM ai_state_meta WHERE key = ?"
SET_STATE_META = ("INSERT INTO ai_state_meta (key, value) VALUES (?, ?) "
                  "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")


def session_analytics(scope: "Scope", day_expression: str) -> tuple[str, tuple]:
    where, params = compose_where(scope, ["started_at > ?"], table="ai_sessions")
    return (
        f"SELECT {day_expression} AS day, COUNT(*) AS sessions, "
        "SUM(uncached_input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, SUM(reasoning_tokens) AS reasoning_tokens, "
        f"SUM(cost_usd) AS cost FROM ai_sessions {where} GROUP BY day ORDER BY day",
        params,
    )


def recent_session_messages(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["m.role = ?"], table="s")
    return (
        "SELECT m.session_id, m.role, m.content, m.created_at, s.title AS session_title "
        "FROM ai_session_messages m JOIN ai_sessions s ON s.id = m.session_id "
        f"{where} ORDER BY m.created_at DESC LIMIT ?",
        params,
    )


DELETE_SESSION_MESSAGES = "DELETE FROM ai_session_messages WHERE session_id = ?"
DELETE_SESSION_USAGE = "DELETE FROM ai_session_usage WHERE session_id = ?"
DELETE_SESSION_ROUTES = "DELETE FROM ai_session_routes WHERE session_id = ?"
DELETE_SESSION_CONTEXTS = "DELETE FROM ai_session_contexts WHERE session_id = ?"
DELETE_SESSION_PROMPT_SNAPSHOTS = "DELETE FROM ai_session_prompt_snapshots WHERE session_id = ?"
DELETE_SESSION_PARTICIPANTS = "DELETE FROM ai_session_participants WHERE session_id = ?"


def touch_session(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"], table="ai_sessions")
    return f"UPDATE ai_sessions SET last_active = ? {where}", params


# Optional mail intake sample.
MAIL_THREAD = "SELECT thread_id FROM fnd_emails WHERE id = ?"
MAIL_INSERT = (
    "INSERT INTO fnd_emails (id, account, folder, uid, thread_id, in_reply_to, references_ids, "
    "from_addr, to_addr, subject, date_header, body_text, raw_headers, direction, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING"
)
MAIL_CLASSIFY = (
    "INSERT INTO fnd_email_classifications (email_id, label, confidence, source, created_at) "
    "VALUES (?,?,?,?,?) ON CONFLICT (email_id, label) DO NOTHING"
)
MAIL_SET_FLOW_MAPPING = (
    "INSERT INTO fnd_classification_flow_map (label, flow_name, enabled, parameters, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?) ON CONFLICT (label) DO UPDATE SET flow_name=EXCLUDED.flow_name, "
    "enabled=EXCLUDED.enabled, parameters=EXCLUDED.parameters, updated_at=EXCLUDED.updated_at"
)
MAIL_GET_FLOW_MAPPING = "SELECT * FROM fnd_classification_flow_map WHERE label = ?"
MAIL_LIST_FLOW_MAPPINGS = "SELECT * FROM fnd_classification_flow_map ORDER BY label"
MAIL_RECORD_DISPATCH = (
    "INSERT INTO fnd_email_dispatches (id, email_id, label, flow_name, run_id, status, detail, created_at) "
    "VALUES (?,?,?,?,?,?,?,?)"
)


def search_sessions_by_title(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["title LIKE ?"], table="ai_sessions")
    return (f"SELECT * FROM ai_sessions {where} "
            f"ORDER BY last_active DESC LIMIT ?"), params


def delete_session(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"], table="ai_sessions")
    return f"DELETE FROM ai_sessions {where}", params


def update_session_title(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"], table="ai_sessions")
    return f"UPDATE ai_sessions SET title = ? {where}", params


def end_session(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"], table="ai_sessions")
    return f"UPDATE ai_sessions SET ended_at = ? {where}", params


def add_session_tokens(scope: "Scope") -> tuple[str, tuple]:
    """Accumulate a call's usage onto the session's running totals.

    The per-call row in `ai_session_usage` is the record of truth; these columns
    exist so listing ai_sessions does not need an aggregate per row.
    """
    where, params = compose_where(scope, ["id = ?"], table="ai_sessions")
    return (
        "UPDATE ai_sessions SET "
        "uncached_input_tokens = uncached_input_tokens + ?, "
        "cache_read_tokens = cache_read_tokens + ?, "
        "cache_write_tokens = cache_write_tokens + ?, "
        "output_tokens = output_tokens + ?, "
        "reasoning_tokens = reasoning_tokens + ?, "
        "cost_usd = cost_usd + ?, "
        f"last_active = ? {where}"
    ), params


def session_messages(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["session_id = ?"],
                                  table="ai_session_messages")
    return (f"SELECT * FROM ai_session_messages {where} ORDER BY seq"), params


def next_message_seq(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["session_id = ?"],
                                  table="ai_session_messages")
    return (f"SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM ai_session_messages "
            f"{where}"), params


def next_usage_seq(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["session_id = ?"],
                                  table="ai_session_usage")
    return (f"SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM ai_session_usage "
            f"{where}"), params


def session_usage_rows(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["session_id = ?"],
                                  table="ai_session_usage")
    return f"SELECT * FROM ai_session_usage {where} ORDER BY seq", params


def usage_by_model(scope: "Scope") -> tuple[str, tuple]:
    """Spend per model across everything the caller may see."""
    where, params = compose_where(scope, ["created_at >= ?"],
                                  table="ai_session_usage")
    return (
        "SELECT model, COUNT(*) AS calls, "
        "SUM(uncached_input_tokens) AS uncached_input_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_write_tokens) AS cache_write_tokens, "
        "SUM(output_tokens) AS output_tokens, "
        "SUM(reasoning_tokens) AS reasoning_tokens, "
        "SUM(cost_usd) AS cost_usd "
        f"FROM ai_session_usage {where} GROUP BY model ORDER BY cost_usd DESC"
    ), params


_TOKENOMICS_DIMENSIONS = {
    "none": "'All'",
    "user": "u.user_id || ' — ' || COALESCE(NULLIF(au.display_name, ''), 'User')",
    "group": "COALESCE(NULLIF(cg.name, ''), s.group_id, 'Unassigned')",
    "department": "COALESCE(NULLIF(ua.department, ''), 'Unassigned')",
    "business_unit": "COALESCE(NULLIF(ua.business_unit, ''), 'Unassigned')",
    "model": "COALESCE(NULLIF(u.model, ''), 'Unknown model')",
    "provider": "COALESCE(NULLIF(u.provider, ''), 'Unknown provider')",
}


def tokenomics_report(
    scope: "Scope",
    period_expr: str,
    dimension: str,
    pivot: str,
) -> tuple[str, tuple]:
    """Aggregated usage cells for Tokenomics report/pivot views.

    Dimension names are allowlisted here; they cannot supply SQL identifiers.
    `period_expr` comes from the dialect's validated UTC date bucketer.
    """
    if dimension not in _TOKENOMICS_DIMENSIONS or pivot not in _TOKENOMICS_DIMENSIONS:
        raise ValueError("unsupported Tokenomics report dimension")
    dimension_expr = _TOKENOMICS_DIMENSIONS[dimension]
    pivot_expr = _TOKENOMICS_DIMENSIONS[pivot]
    where, params = compose_where(
        scope,
        ["u.created_at >= ?", "u.created_at < ?"],
        table="u",
    )
    return (
        "SELECT "
        f"{period_expr} AS period, {dimension_expr} AS \"dimension\", "
        f"{pivot_expr} AS \"pivot\", COUNT(*) AS calls, "
        "SUM(u.uncached_input_tokens + u.cache_read_tokens + "
        "u.cache_write_tokens) AS input_tokens, "
        "SUM(u.output_tokens) AS output_tokens, "
        "SUM(u.reasoning_tokens) AS reasoning_tokens, "
        "SUM(u.cost_usd) AS cost_usd "
        "FROM ai_session_usage u "
        "LEFT JOIN ai_sessions s ON s.id = u.session_id "
        "AND s.tenant_id = u.tenant_id "
        "AND s.environment_id = u.environment_id "
        "LEFT JOIN ai_conversation_groups cg ON cg.id = s.group_id "
        "AND cg.tenant_id = s.tenant_id "
        "AND cg.environment_id = s.environment_id "
        "LEFT JOIN fnd_auth_membership_attributes ua ON ua.user_id = u.user_id "
        "AND ua.tenant_id = u.tenant_id "
        "LEFT JOIN fnd_auth_users au ON au.id = u.user_id "
        f"{where} "
        f"GROUP BY {period_expr}, {dimension_expr}, {pivot_expr} "
        "ORDER BY period, \"dimension\", cost_usd DESC"
    ), params


def search_messages(scope: "Scope", match: str) -> tuple[str, tuple]:
    """Full-text search over message content, joined to its session.

    `match` is a WHERE fragment produced by `Dialect.fulltext_match` — a
    string, not a dialect object, so this module stays free of backend types
    and every builder here keeps the same shape: scope in, (sql, params) out.
    FTS5 and `tsvector` share no syntax, and a backend with neither degrades
    to `LIKE` rather than returning nothing.

    Scoped through `ai_session_messages` itself: message text is the most
    sensitive thing in this database, so an unfiltered search would be the
    widest possible leak.
    """
    where, params = compose_where(scope, [match], table="ai_session_messages")
    return (
        "SELECT m.*, s.title AS session_title "
        "FROM ai_session_messages m "
        "JOIN ai_sessions s ON s.id = m.session_id "
        f"{where.replace('ai_session_messages.', 'm.')} "
        "ORDER BY m.created_at DESC LIMIT ?"
    ), params
