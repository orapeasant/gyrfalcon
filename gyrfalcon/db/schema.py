"""Table definitions, written once and rendered per backend.

Spec: §15.5. The drift risk §15.3 rejects is two hand-maintained copies of the
schema; the fix is one definition and a `type_map()`. A column added here
appears on both backends or on neither.

The vocabulary is deliberately tiny — TEXT, INTEGER, REAL, BOOL — because
that is all this schema actually uses. Anything needing a richer type belongs
in a JSON-encoded TEXT column, which is what `parameters`, `tags`, `payload`,
and `state_details` already are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from gyrfalcon.db.base import Dialect


@dataclass(frozen=True)
class Column:
    name: str
    type: str                      # TEXT | INTEGER | REAL | BOOL
    constraints: str = ""          # "PRIMARY KEY", "NOT NULL DEFAULT 0", ...


@dataclass(frozen=True)
class Index:
    name: str
    table: str
    expr: str                      # "flow_name" or "created_at DESC"


@dataclass(frozen=True)
class Table:
    name: str
    columns: Sequence[Column]
    table_constraints: Sequence[str] = field(default_factory=tuple)
    indexes: Sequence[Index] = field(default_factory=tuple)

    def create_sql(self, dialect: "Dialect") -> str:
        types = dialect.type_map()
        parts = [
            f"    {c.name} {types[c.type]}" + (f" {c.constraints}" if c.constraints else "")
            for c in self.columns
        ]
        parts.extend(f"    {tc}" for tc in self.table_constraints)
        body = ",\n".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n{body}\n)"


# --------------------------------------------------------------------------
# flow runs — the durable record every UI and API reads from (§13.5)
# --------------------------------------------------------------------------

FLOW_RUNS = Table(
    name="flow_runs",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("name", "TEXT", "NOT NULL"),
        Column("kind", "TEXT", "NOT NULL"),          # 'flow' | 'task'
        Column("state_type", "TEXT", "NOT NULL"),
        Column("state_name", "TEXT"),
        Column("parameters", "TEXT"),
        Column("result", "TEXT"),
        Column("error", "TEXT"),
        Column("parent_run_id", "TEXT"),
        Column("flow_run_id", "TEXT"),               # owning flow run, for task runs
        Column("tags", "TEXT"),
        Column("retries", "INTEGER", "DEFAULT 0"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("started_at", "REAL"),
        Column("updated_at", "REAL", "NOT NULL"),
        Column("finished_at", "REAL"),
        # Which engine instance is executing this run, and when it last said
        # so (§15.6.1). NULL on both means nobody is: either the run is
        # terminal, or its owner released it, or it was never claimed.
        Column("owner_id", "TEXT"),
        Column("heartbeat_at", "REAL"),
        # Who this belongs to (§17.4). NOT NULL with a `local` default: a
        # single-user install has no principal to supply and must keep
        # working, and a row that somehow escapes without one lands in a
        # sentinel tenant rather than a real customer's.
        Column("user_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    indexes=(
        Index("idx_runs_state", "flow_runs", "state_type"),
        Index("idx_runs_created", "flow_runs", "created_at DESC"),
        Index("idx_runs_parent", "flow_runs", "parent_run_id"),
        Index("idx_runs_flow", "flow_runs", "flow_run_id"),
        Index("idx_runs_heartbeat", "flow_runs", "heartbeat_at"),
        # Every list query in the dashboard is tenant-scoped, so the plain
        # created_at index alone would force a scan across all tenants.
        Index("idx_runs_owner", "flow_runs", "tenant_id, user_id, created_at DESC"),
    ),
)

# Every proposed transition, accepted or not. This is the audit trail.
#
# There is no surrogate `id` column: (run_id, seq) is the real key, and the
# AUTOINCREMENT integer that used to sit here was never read by any query.
# Dropping it also removes the one place this schema would have needed a
# SERIAL/AUTOINCREMENT type mapping, which the two backends spell differently.
FLOW_RUN_STATES = Table(
    name="flow_run_states",
    columns=(
        Column("run_id", "TEXT", "NOT NULL"),
        Column("seq", "INTEGER", "NOT NULL"),
        Column("state_type", "TEXT", "NOT NULL"),
        Column("state_name", "TEXT"),
        Column("message", "TEXT"),
        Column("state_details", "TEXT"),
        Column("orchestration", "TEXT"),             # ACCEPT | REJECT | ABORT | WAIT
        Column("at", "REAL", "NOT NULL"),
    ),
    table_constraints=("PRIMARY KEY (run_id, seq)",),
    indexes=(Index("idx_states_run", "flow_run_states", "run_id, seq"),),
)

FLOW_RUN_EDGES = Table(
    name="flow_run_edges",
    columns=(
        Column("downstream", "TEXT", "NOT NULL"),
        Column("upstream", "TEXT", "NOT NULL"),
        Column("kind", "TEXT", "NOT NULL DEFAULT 'data'"),  # data | wait_for | encapsulating
    ),
    table_constraints=("PRIMARY KEY (downstream, upstream, kind)",),
)

RUN_TABLES = (FLOW_RUNS, FLOW_RUN_STATES, FLOW_RUN_EDGES)


# --------------------------------------------------------------------------
# events (§10)
# --------------------------------------------------------------------------

FLOW_EVENTS = Table(
    name="flow_events",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("occurred", "REAL", "NOT NULL"),
        Column("event", "TEXT", "NOT NULL"),
        Column("resource_id", "TEXT", "NOT NULL"),
        Column("resource", "TEXT", "NOT NULL"),
        Column("related", "TEXT", "NOT NULL"),
        Column("payload", "TEXT", "NOT NULL"),
        Column("follows", "TEXT"),
        # Who this belongs to (§17.4). NOT NULL with a `local` default: a
        # single-user install has no principal to supply and must keep
        # working, and a row that somehow escapes without one lands in a
        # sentinel tenant rather than a real customer's.
        Column("user_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    indexes=(
        Index("idx_events_owner", "flow_events", "tenant_id, occurred DESC"),
        Index("idx_events_occurred", "flow_events", "occurred DESC"),
        Index("idx_events_type", "flow_events", "event"),
        Index("idx_events_resource", "flow_events", "resource_id"),
    ),
)

EVENT_TABLES = (FLOW_EVENTS,)


# --------------------------------------------------------------------------
# deployments (§9.1)
# --------------------------------------------------------------------------

FLOW_DEPLOYMENTS = Table(
    name="flow_deployments",
    columns=(
        Column("id", "TEXT", "PRIMARY KEY"),
        Column("name", "TEXT", "NOT NULL UNIQUE"),
        Column("flow_name", "TEXT", "NOT NULL"),
        Column("schedule_raw", "TEXT"),
        Column("schedule", "TEXT"),
        Column("parameters", "TEXT", "NOT NULL DEFAULT '{}'"),
        Column("tags", "TEXT", "NOT NULL DEFAULT '[]'"),
        Column("concurrency_limit", "INTEGER"),
        Column("enforce_parameter_schema", "BOOL", "NOT NULL DEFAULT 0"),
        Column("paused", "BOOL", "NOT NULL DEFAULT 0"),
        Column("next_run_at", "TEXT"),
        Column("created_at", "REAL", "NOT NULL"),
        Column("updated_at", "REAL", "NOT NULL"),
        # Who this belongs to (§17.4). NOT NULL with a `local` default: a
        # single-user install has no principal to supply and must keep
        # working, and a row that somehow escapes without one lands in a
        # sentinel tenant rather than a real customer's.
        Column("user_id", "TEXT", "NOT NULL DEFAULT 'local'"),
        Column("tenant_id", "TEXT", "NOT NULL DEFAULT 'local'"),
    ),
    indexes=(
        Index("idx_deployments_flow", "flow_deployments", "flow_name"),
        Index("idx_deployments_owner", "flow_deployments", "tenant_id, name"),
    ),
)

DEPLOYMENT_TABLES = (FLOW_DEPLOYMENTS,)


# --------------------------------------------------------------------------
# identity (§17.11 step 8)
#
# These are not flow tables, and this module's name is now a little narrow for
# what it holds. They live here anyway because they share one database, one
# dialect layer, and — critically — one migration version: splitting them out
# would mean two version counters for one physical schema, which is how a
# half-migrated database happens. Renaming the package is the tidier fix and
# is not worth the churn today.
# --------------------------------------------------------------------------

#: A customer. Gyrfalcon owns this concept rather than inheriting the IdP's
#: tenant, so one deployment can serve several organizations and an org can
#: outlive a change of identity provider.
AUTH_ORGS = Table(
    name="auth_orgs",
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
    name="auth_users",
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
    indexes=(Index("idx_users_email", "auth_users", "email"),),
)

#: Which orgs a person belongs to, and what they may do in each. Roles are
#: per-membership, not per-user: being an operator at one customer must not
#: make you one everywhere.
AUTH_MEMBERSHIPS = Table(
    name="auth_memberships",
    columns=(
        Column("user_id", "TEXT", "NOT NULL"),
        Column("org_id", "TEXT", "NOT NULL"),
        Column("roles", "TEXT", "NOT NULL DEFAULT '[]'"),
        Column("created_at", "REAL", "NOT NULL"),
        # The role this person last selected. Server-side rather than in the
        # browser: the choice must survive a new device, and a CLIENT fetching
        # its menu from a SERVER needs the SERVER to already know the answer.
        Column("active_role", "TEXT"),
    ),
    table_constraints=("PRIMARY KEY (user_id, org_id)",),
    indexes=(Index("idx_memberships_org", "auth_memberships", "org_id"),),
)

#: Per-user credentials for non-interactive callers (§17.3). Only the hash is
#: stored — a leaked database must not yield working keys — with a short
#: non-secret prefix kept so a key can be looked up without scanning.
AUTH_API_KEYS = Table(
    name="auth_api_keys",
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
        Index("idx_api_keys_prefix", "auth_api_keys", "prefix"),
        Index("idx_api_keys_client", "auth_api_keys", "client_id"),
        Index("idx_api_keys_user", "auth_api_keys", "user_id"),
    ),
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
    name="nav_pages",
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
    name="nav_functions",
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
    indexes=(Index("idx_functions_tenant", "nav_functions", "tenant_id"),),
)

NAV_MENUS = Table(
    name="nav_menus",
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
    indexes=(Index("idx_nav_menus_tenant", "nav_menus", "tenant_id"),),
)

#: A leaf (references a Function) or a branch (references another Menu, whole).
#: Exactly one of `function_id` / `ref_menu_id` is set — enforced in the store
#: rather than as a CHECK constraint, to keep one SQL dialect serving both
#: backends (§15.3a).
NAV_MENU_ITEMS = Table(
    name="nav_menu_items",
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
        Index("idx_menu_items_menu", "nav_menu_items", "menu_id"),
        Index("idx_menu_items_function", "nav_menu_items", "function_id"),
        Index("idx_menu_items_ref", "nav_menu_items", "ref_menu_id"),
    ),
)

#: A named bundle of access carrying exactly one Menu. `name` matches a string
#: a Principal already carries in `auth_memberships.roles`, which is what turns
#: that free-form string into a resolvable tree.
AUTH_ROLES = Table(
    name="auth_roles",
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
    indexes=(Index("idx_roles_tenant", "auth_roles", "tenant_id"),),
)

#: Bumped on every write to any nav_* / auth_roles row in a tenant, in the same
#: transaction as the write. The grant cache compares this integer instead of
#: re-walking a menu tree, so a revoked role takes effect on the next request
#: across every process rather than after a TTL.
NAV_VERSIONS = Table(
    name="nav_versions",
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
# These live beside flow, nav and auth rather than in `gyrfalcon_state.py`
# because a server install needs them tenant-scoped, and `sessions` was the one
# substantial data set outside `Scope`. CLIENT mode still resolves to SQLite
# (`db/__init__.py`), so a single-user box gains tenancy columns it never has
# to think about.

SESSIONS = Table(
    name="sessions",
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
    ),
    indexes=(
        Index("idx_sessions_owner", "sessions", "tenant_id, last_active DESC"),
        Index("idx_sessions_user", "sessions", "user_id, last_active DESC"),
        Index("idx_sessions_parent", "sessions", "parent_session_id"),
    ),
)

SESSION_MESSAGES = Table(
    name="session_messages",
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
    ),
    indexes=(
        Index("idx_session_messages_session", "session_messages",
              "session_id, seq"),
        Index("idx_session_messages_owner", "session_messages",
              "tenant_id, created_at DESC"),
    ),
)

SESSION_USAGE = Table(
    name="session_usage",
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
    ),
    indexes=(
        Index("idx_session_usage_session", "session_usage", "session_id, seq"),
        Index("idx_session_usage_owner", "session_usage",
              "tenant_id, created_at DESC"),
        Index("idx_session_usage_model", "session_usage", "model"),
    ),
)

SESSION_TABLES = (SESSIONS, SESSION_MESSAGES, SESSION_USAGE)


ALL_TABLES = (RUN_TABLES + EVENT_TABLES + DEPLOYMENT_TABLES + AUTH_TABLES
              + NAV_TABLES + SESSION_TABLES)


def render(tables: Sequence[Table], dialect: "Dialect") -> list[str]:
    """DDL for `tables`, as separate statements.

    Separate rather than one script because PostgreSQL will not take a
    multi-statement string through the same path SQLite's `executescript`
    uses (§15.4).
    """
    out: list[str] = []
    for table in tables:
        out.append(table.create_sql(dialect))
        for idx in table.indexes:
            out.append(
                f"CREATE INDEX IF NOT EXISTS {idx.name} ON {idx.table}({idx.expr})"
            )
    return out
