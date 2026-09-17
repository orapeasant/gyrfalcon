"""The query catalog — every statement the flow stores run, in one place.

Spec: §15.5.

Why this file exists: with one backend, SQL scattered across `store.py`,
`events.py`, and `deployments.py` was fine. With two, any statement is
potentially dialect-sensitive, and three separate homes means portability gets
re-audited in three places on every change. Collected here, the whole database
surface is one file you can read top to bottom, and the handful of genuinely
backend-specific statements are visibly exceptional rather than buried.

What this is *not*: an ORM, or the beginnings of one (§15.3). These are strings
and parameter orders. There are no row objects, no identity map, no query
builder DSL. The stores keep every bit of behaviour — `record_transition`
still decides what a transition *means*; it just stopped owning the text of
the INSERT.

Two rules keep it that way:

1. This module imports nothing from `gyrfalcon.flow` outside `db/`. It has no
   knowledge of `State`, `StateType`, or `FlowRun`. Callers pass values.
2. A function appears here only when a statement must ask the `Dialect`, or
   when its shape depends on a runtime count (an `IN (?,?,?)` list). Anything
   that can be a constant is a constant.

Consequence of rule 1 worth knowing: state-name lists (`TERMINAL_STATES` and
friends) are *never* inlined as SQL literals here. They arrive as bound
parameters from `states.py`, which is the single source of truth for them —
so the §15.12 drift warning is answered by there being no second copy at all,
rather than by this file holding the only one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from gyrfalcon.flow.db.scope import Scope, merge

if TYPE_CHECKING:
    from gyrfalcon.flow.db.base import Dialect


def compose_where(scope: "Scope", clauses: Sequence[str] = (),
                  table: str = "") -> tuple[str, tuple]:
    """The one place a WHERE clause is assembled (§17.5).

    Every read below routes through this, so "is this query tenant-scoped" is
    a property of one function rather than of thirty call sites.
    """
    return merge(scope, clauses, table)


def placeholders(n: int) -> str:
    """`?,?,?` for an IN list of `n` bound values.

    Portable as-is: pg8000 is configured with `paramstyle = "qmark"`, so `?`
    is the placeholder on both backends and this needs no dialect (§15.3a).
    """
    return ",".join("?" * n)


# ==========================================================================
# flow_runs  (store.py)
# ==========================================================================

RUN_COLUMNS = (
    "id", "name", "kind", "state_type", "state_name", "parameters",
    "parent_run_id", "flow_run_id", "tags", "created_at", "updated_at",
    "owner_id", "heartbeat_at", "user_id", "tenant_id",
)


def insert_run(d: "Dialect") -> str:
    """New run rows are idempotent: the engine may re-enter a run body after a
    pause or a retry, and re-creating an existing row must be a no-op."""
    return d.insert_ignore("flow_runs", RUN_COLUMNS, conflict="id")


def get_run(scope: "Scope") -> tuple[str, tuple]:
    """One run by id — scoped, because an id is guessable-adjacent and
    "fetch by primary key" is exactly where a forgotten filter hides."""
    where, params = compose_where(scope, ["id = ?"])
    return f"SELECT * FROM flow_runs {where}", params


def run_kind_and_name(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"])
    return f"SELECT kind, name FROM flow_runs {where}", params


def counts_by_state(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope)
    return (
        f"SELECT state_type, COUNT(*) AS c FROM flow_runs {where} GROUP BY state_type",
        params,
    )


def runs_created_since(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["created_at >= ?"])
    return f"SELECT state_type, created_at FROM flow_runs {where}", params


def graph_nodes(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(
        scope, ["(id = ? OR flow_run_id = ? OR parent_run_id = ?)"]
    )
    return f"SELECT * FROM flow_runs {where}", params

APPLY_TRANSITION = """
    UPDATE flow_runs
       SET state_type   = ?,
           state_name   = ?,
           updated_at   = ?,
           started_at   = COALESCE(started_at, ?),
           finished_at  = ?,
           result       = COALESCE(?, result),
           error        = COALESCE(?, error),
           owner_id     = ?,
           heartbeat_at = ?
     WHERE id = ?
"""

MARK_CRASHED = """
    UPDATE flow_runs
       SET state_type = ?, state_name = ?, updated_at = ?, finished_at = ?, error = ?
     WHERE id = ?
"""


# ── ownership and liveness (§15.6.1) ──────────────────────────────────────

#: Refresh the heartbeat on every run this instance still owns. One statement
#: per interval regardless of how many runs are in flight.
TOUCH_OWNED_RUNS = """
    UPDATE flow_runs SET heartbeat_at = ?
     WHERE owner_id = ? AND finished_at IS NULL
"""

#: Clean shutdown: drop ownership so a peer can reclaim immediately rather
#: than waiting out the stale threshold. Unclean death skips this, which is
#: exactly what the threshold is for.
RELEASE_OWNED_RUNS = """
    UPDATE flow_runs SET owner_id = NULL, heartbeat_at = NULL
     WHERE owner_id = ? AND finished_at IS NULL
"""


def abandoned_runs(scope: "Scope", n_states: int) -> tuple[str, tuple]:
    """Non-terminal runs no live engine is heartbeating.

    The `heartbeat_at IS NULL OR heartbeat_at < ?` test is the whole fix for
    §15.6.1: a peer's *live* run has a fresh heartbeat and is left alone, so a
    second process starting no longer marks another machine's work Crashed.

    Normally called with `Scope.system(...)`: reconciliation settles abandoned
    work in every tenant, because a crashed process took its tenants' runs
    down with it regardless of who owned them.
    """
    where, params = compose_where(
        scope,
        [f"state_type IN ({placeholders(n_states)})",
         "(heartbeat_at IS NULL OR heartbeat_at < ?)"],
    )
    return f"SELECT id FROM flow_runs {where}", params


def runs_in_states(scope: "Scope", n: int) -> tuple[str, tuple]:
    """Run ids currently sitting in any of `n` given states.

    The states themselves come from `states.py`.
    """
    where, params = compose_where(scope, [f"state_type IN ({placeholders(n)})"])
    return f"SELECT id FROM flow_runs {where}", params


def count_active_by_name(scope: "Scope", n_terminal: int) -> tuple[str, tuple]:
    """Exact-name count of non-terminal runs, for concurrency limits.

    Exact `name = ?` rather than the `LIKE %name%` the search UI uses: a fuzzy
    match would over-count `ingest` against `ingest_v2` and throttle a flow
    that never hit its limit.
    """
    where, params = compose_where(
        scope, ["name = ?", f"state_type NOT IN ({placeholders(n_terminal)})"]
    )
    return f"SELECT COUNT(*) AS c FROM flow_runs {where}", params


def count_active_for_tenant(scope: "Scope", n_terminal: int) -> tuple[str, tuple]:
    """Non-terminal runs anywhere in the caller's tenant (§17.8).

    Deliberately not per flow name: the noisy-neighbour cap is about a tenant's
    total footprint, which is what `count_active_by_name` cannot see.
    """
    where, params = compose_where(
        scope, [f"state_type NOT IN ({placeholders(n_terminal)})"]
    )
    return f"SELECT COUNT(*) AS c FROM flow_runs {where}", params


def list_runs(where: str) -> str:
    """`where` comes from `compose_where`; the store never builds one itself."""
    return f"SELECT * FROM flow_runs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"


def count_runs(where: str) -> str:
    return f"SELECT COUNT(*) AS c FROM flow_runs {where}"


def delete_run(scope: "Scope") -> tuple[str, tuple]:
    """Scoped for the same reason `get_run` is (§17.5): a delete by bare id
    would let one tenant's request remove another tenant's row."""
    where, params = compose_where(scope, ["id = ?"])
    return f"DELETE FROM flow_runs {where}", params


# ==========================================================================
# flow_run_states — the transition audit trail
# ==========================================================================

NEXT_SEQ = "SELECT COALESCE(MAX(seq), 0) AS s FROM flow_run_states WHERE run_id = ?"

INSERT_RUN_STATE = """
    INSERT INTO flow_run_states
        (run_id, seq, state_type, state_name, message, state_details, orchestration, at)
    VALUES (?,?,?,?,?,?,?,?)
"""

GET_HISTORY = "SELECT * FROM flow_run_states WHERE run_id = ? ORDER BY seq"
DELETE_HISTORY = "DELETE FROM flow_run_states WHERE run_id = ?"


# ==========================================================================
# flow_run_edges — the run graph
# ==========================================================================

EDGE_COLUMNS = ("downstream", "upstream", "kind")


def insert_edge(d: "Dialect") -> str:
    """The same dependency can be observed more than once (a task awaited
    twice); recording it again must not raise."""
    return d.insert_ignore("flow_run_edges", EDGE_COLUMNS, conflict="downstream, upstream, kind")


def edges_for(n_ids: int) -> str:
    return f"SELECT * FROM flow_run_edges WHERE downstream IN ({placeholders(n_ids)})"


DELETE_EDGES_FOR = "DELETE FROM flow_run_edges WHERE downstream = ? OR upstream = ?"


# ==========================================================================
# flow_events  (events.py, §10)
# ==========================================================================

INSERT_EVENT = """
    INSERT INTO flow_events
        (id, occurred, event, resource_id, resource, related, payload, follows,
         user_id, tenant_id)
    VALUES (?,?,?,?,?,?,?,?,?,?)
"""

def get_event(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"])
    return f"SELECT * FROM flow_events {where}", params


def list_events(where: str) -> str:
    return f"SELECT * FROM flow_events {where} ORDER BY occurred DESC LIMIT ? OFFSET ?"


def count_events(where: str) -> str:
    return f"SELECT COUNT(*) AS c FROM flow_events {where}"


# ==========================================================================
# flow_deployments  (deployments.py, §9.1)
# ==========================================================================

INSERT_DEPLOYMENT = """
    INSERT INTO flow_deployments
        (id, name, flow_name, schedule_raw, schedule, parameters, tags,
         concurrency_limit, enforce_parameter_schema, paused, next_run_at,
         created_at, updated_at, user_id, tenant_id)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

def get_deployment(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["id = ?"])
    return f"SELECT * FROM flow_deployments {where}", params


def get_deployment_by_name(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope, ["name = ?"])
    return f"SELECT * FROM flow_deployments {where}", params


def list_deployments(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope)
    return f"SELECT * FROM flow_deployments {where} ORDER BY name", params
DELETE_DEPLOYMENT = "DELETE FROM flow_deployments WHERE id = ?"

ADVANCE_SCHEDULE = (
    "UPDATE flow_deployments SET next_run_at = ?, updated_at = ? WHERE id = ?"
)

#: Compare-and-swap claim (§15.6.2). Advancing `next_run_at` only if it still
#: holds the value we read *is* the claim: exactly one racer's UPDATE reports
#: a row, and the losers see rowcount 0 and skip the tick. Identical on both
#: backends, which `FOR UPDATE SKIP LOCKED` would not have been.
CLAIM_DEPLOYMENT = """
    UPDATE flow_deployments SET next_run_at = ?, updated_at = ?
     WHERE id = ? AND next_run_at = ?
"""

#: The same claim for a deployment with no scheduled time (an unscheduled or
#: manually-fired one). `= ?` can never match NULL, so without this branch such
#: a deployment would be dropped by every claim — silently, which is the worst
#: way for a scheduler to fail. `IS NULL` is standard on both backends;
#: `IS ?` is not (PostgreSQL spells that `IS NOT DISTINCT FROM`).
CLAIM_UNSCHEDULED_DEPLOYMENT = """
    UPDATE flow_deployments SET next_run_at = ?, updated_at = ?
     WHERE id = ? AND next_run_at IS NULL
"""

SET_PAUSED = (
    "UPDATE flow_deployments SET paused = ?, next_run_at = ?, updated_at = ? WHERE id = ?"
)

#: Editing a deployment (name/schedule/parameters/tags/concurrency), not its
#: run-time bookkeeping (`paused`, `next_run_at` are untouched here — a
#: schedule *string* change recomputes `next_run_at` in Python, same as
#: `create`, and is passed in explicitly rather than derived in SQL).
UPDATE_DEPLOYMENT = """
    UPDATE flow_deployments
       SET name = ?, schedule_raw = ?, schedule = ?, parameters = ?, tags = ?,
           concurrency_limit = ?, next_run_at = ?, updated_at = ?
     WHERE id = ?
"""


def select_due_deployments(d: "Dialect", where: str = "") -> str:
    """Rows a runner intends to claim and fire.

    On PostgreSQL this must skip rows another runner has already locked, or
    two processes fire the same deployment on the same tick (§15.6). SQLite
    has no such clause and needs none — its single write lock already
    serializes the claim.
    """
    sql = f"SELECT * FROM flow_deployments {where} ORDER BY name"
    if d.supports_skip_locked:
        sql += " FOR UPDATE SKIP LOCKED"
    return sql


# ==========================================================================
# identity  (auth/store.py, §17.11 step 8)
#
# These are plain constants, not scope-taking builders, and that is not an
# oversight: they are read *during authentication*, before a principal exists,
# so there is nothing to scope by yet. Scoping the identity store by the
# identity it establishes would be circular. Everything here is reachable only
# from `auth/store.py`, which is the boundary that keeps it honest.
# ==========================================================================

INSERT_ORG = "INSERT INTO auth_orgs (id, name, created_at, disabled) VALUES (?,?,?,0)"
GET_ORG = "SELECT * FROM auth_orgs WHERE id = ?"
GET_ORG_BY_NAME = "SELECT * FROM auth_orgs WHERE name = ?"
LIST_ORGS = "SELECT * FROM auth_orgs ORDER BY name"

INSERT_USER = """
    INSERT INTO auth_users
        (id, issuer, subject, email, display_name, created_at, last_login_at, disabled)
    VALUES (?,?,?,?,?,?,?,0)
"""
GET_USER = "SELECT * FROM auth_users WHERE id = ?"
FIND_USER = "SELECT * FROM auth_users WHERE issuer = ? AND subject = ?"
TOUCH_USER = """
    UPDATE auth_users SET email = ?, display_name = ?, last_login_at = ?
     WHERE id = ?
"""
LIST_USERS = "SELECT * FROM auth_users ORDER BY created_at"

GET_MEMBERSHIP = "SELECT * FROM auth_memberships WHERE user_id = ? AND org_id = ?"
LIST_MEMBERSHIPS = "SELECT * FROM auth_memberships WHERE user_id = ? ORDER BY org_id"
LIST_ORG_MEMBERS = "SELECT * FROM auth_memberships WHERE org_id = ? ORDER BY user_id"
UPSERT_MEMBERSHIP_UPDATE = (
    "UPDATE auth_memberships SET roles = ? WHERE user_id = ? AND org_id = ?"
)
DELETE_MEMBERSHIP = "DELETE FROM auth_memberships WHERE user_id = ? AND org_id = ?"

MEMBERSHIP_COLUMNS = ("user_id", "org_id", "roles", "created_at")


def insert_membership(d: "Dialect") -> str:
    return d.insert_ignore("auth_memberships", MEMBERSHIP_COLUMNS,
                           conflict="user_id, org_id")


INSERT_API_KEY = """
    INSERT INTO auth_api_keys
        (id, prefix, key_hash, user_id, org_id, name, created_at)
    VALUES (?,?,?,?,?,?,?)
"""
#: Looked up by the non-secret prefix; the hash is then compared in constant
#: time. Finding the row must not itself be the authentication decision.
FIND_API_KEYS_BY_PREFIX = (
    "SELECT * FROM auth_api_keys WHERE prefix = ? AND revoked_at IS NULL"
)
TOUCH_API_KEY = "UPDATE auth_api_keys SET last_used_at = ? WHERE id = ?"
REVOKE_API_KEY = "UPDATE auth_api_keys SET revoked_at = ? WHERE id = ?"
LIST_API_KEYS = (
    "SELECT * FROM auth_api_keys WHERE user_id = ? ORDER BY created_at DESC"
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
    INSERT INTO nav_pages (id, key, route, label, icon, enabled, created_at)
    VALUES (?,?,?,?,?,?,?)
"""
GET_PAGE = "SELECT * FROM nav_pages WHERE id = ?"
GET_PAGE_BY_KEY = "SELECT * FROM nav_pages WHERE key = ?"
LIST_PAGES = "SELECT * FROM nav_pages ORDER BY key"
UPDATE_PAGE = (
    "UPDATE nav_pages SET route = ?, label = ?, icon = ?, enabled = ? WHERE id = ?"
)
DELETE_PAGE = "DELETE FROM nav_pages WHERE id = ?"


def insert_page(d: "Dialect") -> str:
    """Seeding re-runs on every start, so a page that already exists is a
    no-op rather than a duplicate-key failure."""
    return d.insert_ignore("nav_pages", PAGE_COLUMNS, conflict="key")


# -- functions -------------------------------------------------------------

FUNCTION_COLUMNS = (
    "id", "key", "name", "icon", "kind", "target", "params", "enabled",
    "active_from", "active_to", "created_at", "updated_at", "tenant_id",
)


def insert_function(d: "Dialect") -> str:
    return d.insert_ignore("nav_functions", FUNCTION_COLUMNS,
                           conflict="tenant_id, key")


def get_function(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM nav_functions {where}", params


def get_function_by_key(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["key = ?"])
    return f"SELECT * FROM nav_functions {where}", params


def list_functions(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide())
    return f"SELECT * FROM nav_functions {where} ORDER BY name", params


def update_function(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE nav_functions SET name = ?, icon = ?, kind = ?, target = ?, "
        f"params = ?, enabled = ?, active_from = ?, active_to = ?, updated_at = ? {where}",
        params,
    )


def delete_function(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM nav_functions {where}", params


# -- menus -----------------------------------------------------------------

MENU_COLUMNS = ("id", "name", "icon", "enabled", "active_from", "active_to",
                "created_at", "updated_at", "tenant_id")


def insert_menu(d: "Dialect") -> str:
    return d.insert_ignore("nav_menus", MENU_COLUMNS, conflict="id")


def get_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM nav_menus {where}", params


def list_menus(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide())
    return f"SELECT * FROM nav_menus {where} ORDER BY name", params


def update_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE nav_menus SET name = ?, icon = ?, enabled = ?, "
        f"active_from = ?, active_to = ?, updated_at = ? {where}",
        params,
    )


def delete_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM nav_menus {where}", params


# -- menu items ------------------------------------------------------------

MENU_ITEM_COLUMNS = (
    "id", "menu_id", "sort_order", "function_id", "ref_menu_id", "access",
    "label_override", "icon_override", "enabled", "active_from", "active_to",
    "tenant_id",
)


def insert_menu_item(d: "Dialect") -> str:
    return d.insert_ignore("nav_menu_items", MENU_ITEM_COLUMNS, conflict="id")


def get_menu_item(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM nav_menu_items {where}", params


def list_menu_items(scope: "Scope") -> tuple[str, tuple]:
    """One menu's items, in render order. `sort_order` first, `id` as the
    tiebreak so two items sharing an order still come back deterministically
    rather than in whatever order the backend happens to return."""
    where, params = compose_where(scope.tenant_wide(), ["menu_id = ?"])
    return f"SELECT * FROM nav_menu_items {where} ORDER BY sort_order, id", params


def items_referencing_menu(scope: "Scope") -> tuple[str, tuple]:
    """Branches pointing at a given menu — for cycle detection and for
    reporting what would break before a menu is deleted."""
    where, params = compose_where(scope.tenant_wide(), ["ref_menu_id = ?"])
    return f"SELECT * FROM nav_menu_items {where}", params


def update_menu_item(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE nav_menu_items SET sort_order = ?, access = ?, "
        "label_override = ?, icon_override = ?, enabled = ?, "
        f"active_from = ?, active_to = ? {where}",
        params,
    )


def set_menu_item_order(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"UPDATE nav_menu_items SET sort_order = ? {where}", params


def delete_menu_item(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM nav_menu_items {where}", params


def delete_items_of_menu(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["menu_id = ?"])
    return f"DELETE FROM nav_menu_items {where}", params


# -- roles -----------------------------------------------------------------

ROLE_COLUMNS = ("id", "name", "label", "menu_id", "enabled", "active_from",
                "active_to", "description", "created_at", "tenant_id")


def insert_role(d: "Dialect") -> str:
    return d.insert_ignore("auth_roles", ROLE_COLUMNS, conflict="tenant_id, name")


def get_role(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"SELECT * FROM auth_roles {where}", params


def get_role_by_name(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["name = ?"])
    return f"SELECT * FROM auth_roles {where}", params


def list_roles(scope: "Scope") -> tuple[str, tuple]:
    """Ordered by `label` so §4.1's "first live nav-granting role" fallback is
    deterministic — two logins must never disagree about which role is
    active."""
    where, params = compose_where(scope.tenant_wide())
    return f"SELECT * FROM auth_roles {where} ORDER BY label, name", params


def update_role(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return (
        "UPDATE auth_roles SET name = ?, label = ?, menu_id = ?, enabled = ?, "
        f"active_from = ?, active_to = ?, description = ? {where}",
        params,
    )


def delete_role(scope: "Scope") -> tuple[str, tuple]:
    where, params = compose_where(scope.tenant_wide(), ["id = ?"])
    return f"DELETE FROM auth_roles {where}", params


# -- the version counter ---------------------------------------------------
#
# Keyed by tenant and bumped inside the same transaction as any nav write, so
# every process invalidates its grant cache on the next request rather than
# after a TTL. Not a scoped builder: the tenant is the primary key, and the
# cache asks about exactly one.

GET_NAV_VERSION = "SELECT version FROM nav_versions WHERE tenant_id = ?"
BUMP_NAV_VERSION = (
    "UPDATE nav_versions SET version = version + 1, updated_at = ? "
    "WHERE tenant_id = ?"
)
NAV_VERSION_COLUMNS = ("tenant_id", "version", "updated_at")


def insert_nav_version(d: "Dialect") -> str:
    return d.insert_ignore("nav_versions", NAV_VERSION_COLUMNS, conflict="tenant_id")


# -- service accounts on auth_api_keys -------------------------------------

FIND_API_KEY_BY_CLIENT_ID = (
    "SELECT * FROM auth_api_keys WHERE client_id = ? AND revoked_at IS NULL"
)
