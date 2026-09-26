"""Schema versioning — an ordered, forward-only migration list.

Spec: §15.8.

`CREATE TABLE IF NOT EXISTS` is adequate for one file owned by one app version.
It stops being adequate the moment a shared PostgreSQL outlives a deploy,
because `IF NOT EXISTS` *succeeds* against a table with an older shape: the
statement is happy, the schema is wrong, and the mismatch surfaces later as a
confusing runtime error far from its cause.

Three properties make this safe rather than merely present:

1. **Forward only.** There are no down-migrations. Rolling back a schema
   under live data is how data is lost; roll back the *code* and restore from
   a backup instead.
2. **Refuse if the database is newer than the code.** An older process must
   never write to a schema it does not understand — it would write NULLs into
   columns it has never heard of. Fail loudly at startup instead.
3. **One transaction per migration, under a cross-process lock.** A migration
   that fails halfway leaves nothing behind, and two app servers starting at
   once cannot both apply it (§15.6).

One database is one schema at one version. Every store therefore ensures the
*whole* schema, not just its own tables: `flow_runs`, `flow_events`, and
`flow_deployments` share a database, and a per-store version would have no
coherent meaning.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from gyrfalcon.db import legacy_schema as sch
from gyrfalcon.db import schema as current_schema

if TYPE_CHECKING:
    from gyrfalcon.db.base import Connection, Database, Dialect

VERSION_TABLE = "flow_schema_version"
CURRENT_VERSION_TABLE = "fnd_flow_schema_version"


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[["Connection", "Dialect"], None]


# ── v1 — baseline ─────────────────────────────────────────────────────────────

# The schema *as it was at v1*, frozen. See `_v1_baseline` for why this is
# spelled out rather than read from `schema.py`.
_V1_TABLES = ("flow_runs", "flow_run_states", "flow_run_edges",
              "flow_events", "flow_deployments")
_V1_COLUMNS = {
    "flow_runs": (
        "id", "name", "kind", "state_type", "state_name", "parameters",
        "result", "error", "parent_run_id", "flow_run_id", "tags", "retries",
        "created_at", "started_at", "updated_at", "finished_at",
    ),
    "flow_run_states": (
        "run_id", "seq", "state_type", "state_name", "message",
        "state_details", "orchestration", "at",
    ),
    "flow_run_edges": ("downstream", "upstream", "kind"),
    "flow_events": (
        "id", "occurred", "event", "resource_id", "resource", "related",
        "payload", "follows",
    ),
    "flow_deployments": (
        "id", "name", "flow_name", "schedule_raw", "schedule", "parameters",
        "tags", "concurrency_limit", "enforce_parameter_schema", "paused",
        "next_run_at", "created_at", "updated_at",
    ),
}
_V1_INDEXES = {
    "flow_runs": ("idx_runs_state", "idx_runs_created", "idx_runs_parent",
                  "idx_runs_flow"),
    "flow_run_states": ("idx_states_run",),
    "flow_events": ("idx_events_occurred", "idx_events_type",
                    "idx_events_resource"),
    "flow_deployments": ("idx_deployments_flow",),
}


def _as_of_v1(table: "sch.Table") -> "sch.Table":
    return replace(
        table,
        columns=tuple(c for c in table.columns
                      if c.name in _V1_COLUMNS[table.name]),
        indexes=tuple(i for i in table.indexes
                      if i.name in _V1_INDEXES.get(table.name, ())),
    )


def _v1_baseline(conn: "Connection", dialect: "Dialect") -> None:
    """Every table as of the `db/` extraction — *and nothing since*.

    `IF NOT EXISTS` makes this both the fresh-install path and the stamp for a
    database that predates versioning: an existing `flow.db` already has these
    tables, so it applies as a no-op and stays at v1 with its data intact.

    **Why the v1 shape is written out here instead of read from `schema.py`.**
    It used to render `sch.ALL_TABLES` — the *live* schema — and that was
    wrong in a way that only showed up once a later migration added a column.
    `CREATE TABLE IF NOT EXISTS` no-ops against an existing `flow_runs`, but
    the accompanying `CREATE INDEX ... (heartbeat_at)` from v3's column does
    not: it fires unconditionally, before v3 has added the column, and
    `ensure_schema()` dies with "no such column" on the *first* migration.
    That broke opening every database predating v3, on both backends.

    The general rule, and the reason this file must never import the live
    schema for an already-released migration: **a migration is a historical
    fact and cannot be allowed to change when the schema does.** v1 creates
    what v1 created; v3 adds its columns and its index; v4 and v5 add theirs.

    Column *types* are still read from `schema.py`, which is a deliberate
    narrowing of the same risk: changing a column's type is itself a migration,
    so it cannot silently rewrite history the way adding one did.
    """
    tables = tuple(_as_of_v1(t) for t in sch.ALL_TABLES if t.name in _V1_TABLES)
    conn.executescript(sch.render(tables, dialect))


# ── v2 — drop the dead surrogate key on flow_run_states ──────────────────────

def _v2_run_states_natural_key(conn: "Connection", dialect: "Dialect") -> None:
    """Rebuild `flow_run_states` around its real key, `(run_id, seq)`.

    Databases created before the extraction carry an
    `id INTEGER PRIMARY KEY AUTOINCREMENT` column that no query has ever read.
    It is harmless in itself — SQLite keeps filling it — but it means two
    installs have two different shapes, and a version stamp that claims
    otherwise is worse than no stamp at all.

    A column that is part of the primary key cannot be dropped in place on
    either backend, so this is the copy-and-swap that `ALTER TABLE` cannot do.
    Guarded by introspection: on a database already created without `id`
    (anything fresh), it does nothing.
    """
    columns = dialect.column_names(conn, "flow_run_states")
    if not columns or "id" not in columns:
        return

    keep = [c.name for c in sch.FLOW_RUN_STATES.columns]
    cols = ", ".join(keep)
    tmp = "flow_run_states__v2"

    conn.executescript([
        f"DROP TABLE IF EXISTS {tmp}",
        sch.FLOW_RUN_STATES.create_sql(dialect).replace(
            "flow_run_states", tmp, 1
        ),
        # DISTINCT because the old table's UNIQUE(run_id, seq) is exactly the
        # new primary key: any duplicate would abort the swap, and there is no
        # information in a repeated row to preserve.
        f"INSERT INTO {tmp} ({cols}) SELECT DISTINCT {cols} FROM flow_run_states",
        "DROP TABLE flow_run_states",
        f"ALTER TABLE {tmp} RENAME TO flow_run_states",
    ])
    for idx in sch.FLOW_RUN_STATES.indexes:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {idx.name} ON {idx.table}({idx.expr})"
        )


# ── v3 — run ownership, so reconciliation stops being destructive ────────────

def _v3_run_ownership(conn: "Connection", dialect: "Dialect") -> None:
    """Add `owner_id` and `heartbeat_at` to `flow_runs` (§15.6.1).

    Without these, `_reconcile_crashed()` marks *every* non-terminal run
    Crashed when a store opens — correct for one process, destructive the
    moment a second one starts against a shared database, because the other
    machine's live runs get settled as failures.

    Additive `ALTER TABLE ADD COLUMN`, which both backends support and which
    needs no table rebuild. Guarded by introspection so it is a no-op on any
    database created after the columns entered `schema.py`.
    """
    columns = dialect.column_names(conn, "flow_runs")
    if not columns:
        return
    types = dialect.type_map()
    if "owner_id" not in columns:
        conn.execute(f"ALTER TABLE flow_runs ADD COLUMN owner_id {types['TEXT']}")
    if "heartbeat_at" not in columns:
        conn.execute(f"ALTER TABLE flow_runs ADD COLUMN heartbeat_at {types['REAL']}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_heartbeat ON flow_runs(heartbeat_at)"
    )


# ── v4 — tenancy: who owns each row ─────────────────────────────────────────

_OWNED_TABLES = {
    "flow_runs": (
        "CREATE INDEX IF NOT EXISTS idx_runs_owner "
        "ON flow_runs(tenant_id, user_id, created_at DESC)"
    ),
    "flow_events": (
        "CREATE INDEX IF NOT EXISTS idx_events_owner "
        "ON flow_events(tenant_id, occurred DESC)"
    ),
    "flow_deployments": (
        "CREATE INDEX IF NOT EXISTS idx_deployments_owner "
        "ON flow_deployments(tenant_id, name)"
    ),
}


def _v4_tenancy(conn: "Connection", dialect: "Dialect") -> None:
    """Add `user_id`/`tenant_id` to every owned table (§17.4).

    `NOT NULL DEFAULT 'local'` does the backfill in the same statement: every
    pre-existing row becomes the property of the single-user `local` tenant,
    which is exactly what it was. That keeps a personal install working with
    no data migration of its own and no code change, and means switching
    identity on later moves rows between tenants rather than changing what a
    NULL owner means.

    Indexes are tenant-first because every list query is tenant-scoped — a
    plain `created_at` index would make the dashboard scan other tenants'
    rows to find its own.
    """
    types = dialect.type_map()
    for table, index_sql in _OWNED_TABLES.items():
        columns = dialect.column_names(conn, table)
        if not columns:
            continue
        for name in ("user_id", "tenant_id"):
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {types['TEXT']} "
                    f"NOT NULL DEFAULT 'local'"
                )
        conn.execute(index_sql)


# ── v5 — the identity store ─────────────────────────────────────────────────

def _v5_identity(conn: "Connection", dialect: "Dialect") -> None:
    """Orgs, users, memberships and API keys (§17.11 step 8).

    Gyrfalcon owns roles and org membership; the IdP only proves who someone
    is. That keeps role changes out of a directory admin's hands, lets one
    deployment serve several customers, and means an org survives a change of
    identity provider — at the cost of being a second place where access has
    to be kept current, which is the trade that was chosen deliberately.
    """
    conn.executescript(sch.render(sch.AUTH_TABLES, dialect))


# ── v6 — navigation and access control ──────────────────────────────────────

#: Columns this migration adds to tables v5 already created. Additive
#: `ALTER TABLE ADD COLUMN` with a default, which both backends support without
#: a rebuild and which backfills existing rows in the same statement.
_V6_ADDED_COLUMNS = {
    "auth_users": (("kind", "TEXT", "NOT NULL DEFAULT 'human'"),),
    "auth_memberships": (("active_role", "TEXT", ""),),
    "auth_api_keys": (
        ("hash_algo", "TEXT", "NOT NULL DEFAULT 'v1'"),
        ("client_id", "TEXT", ""),
    ),
}


def _v6_navigation(conn: "Connection", dialect: "Dialect") -> None:
    """Pages, functions, menus, menu items, roles, and the version counter.

    Structure only — **no seed rows**. That is a deliberate departure from a
    literal reading of the design, and the reason is this module's own rule:
    a migration is a historical fact and must not change when the schema does.
    Seed data for the default menus is a transcription of the dashboard's nav,
    and that nav grows every time a page ships. Embedding it here would force
    the choice between editing an applied migration (forbidden) and adding one
    migration per new page (absurd). Seeding is therefore idempotent and lives
    in `gyrfalcon/nav/seed.py`, guarded on "does this tenant have roles yet",
    so a fresh install still comes up with a working sidebar.

    `auth_users.kind` defaults to `'human'`, which is the correct reading of
    every row that predates service accounts having their own identity.
    """
    conn.executescript(sch.render(sch.NAV_TABLES, dialect))

    types = dialect.type_map()
    for table, additions in _V6_ADDED_COLUMNS.items():
        existing = dialect.column_names(conn, table)
        if not existing:
            continue
        for name, neutral_type, constraints in additions:
            if name in existing:
                continue
            suffix = f" {constraints}" if constraints else ""
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {types[neutral_type]}{suffix}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_keys_client ON auth_api_keys(client_id)"
    )



def _v7_sessions(conn: "Connection", dialect: "Dialect") -> None:
    """Sessions, their messages, and per-call token usage (§19.7).

    Three tables rather than two: `session_usage` records **one row per LLM
    call**, which is what makes "where did the money go in this session"
    answerable. The pre-existing store kept only running session totals
    (`SET x = x + ?`), so per-call granularity was not merely unqueried, it
    was never written down.

    The full-text index is created through the dialect because SQLite's FTS5
    and PostgreSQL's `tsvector` + GIN have nothing in common but intent
    (§15.4). A backend that offers neither simply gets no index, and search
    falls back to `LIKE` — slower, never wrong.
    """
    # Freeze v7's three-table shape. Newer session columns and tables belong
    # to v8; rendering the live SESSION_TABLES here would make a fresh upgrade
    # create v8's tables before their parent keys exist.
    old_columns = {
        "sessions": (
            "id", "source", "agent_id", "model", "parent_session_id",
            "title", "system_prompt", "started_at", "ended_at",
            "last_active", "uncached_input_tokens", "cache_read_tokens",
            "cache_write_tokens", "output_tokens", "reasoning_tokens",
            "cost_usd", "user_id", "tenant_id",
        ),
        "session_messages": (
            "id", "session_id", "seq", "role", "content", "tool_call_id",
            "tool_calls", "tool_name", "reasoning", "created_at",
            "user_id", "tenant_id",
        ),
        "session_usage": (
            "id", "session_id", "seq", "created_at", "provider", "model",
            "uncached_input_tokens", "cache_read_tokens",
            "cache_write_tokens", "cache_ttl", "output_tokens",
            "reasoning_tokens", "cost_usd", "pricing_catalog_version",
            "user_id", "tenant_id",
        ),
    }
    old_indexes = {
        "sessions": ("idx_sessions_owner", "idx_sessions_user", "idx_sessions_parent"),
        "session_messages": ("idx_session_messages_session", "idx_session_messages_owner"),
        "session_usage": ("idx_session_usage_session", "idx_session_usage_owner",
                          "idx_session_usage_model"),
    }
    tables = tuple(
        replace(
            table,
            columns=tuple(c for c in table.columns if c.name in old_columns[table.name]),
            indexes=tuple(i for i in table.indexes if i.name in old_indexes[table.name]),
            table_constraints=(),
        )
        for table in (sch.SESSIONS, sch.SESSION_MESSAGES, sch.SESSION_USAGE)
    )
    conn.executescript(sch.render(tables, dialect))
    for statement in dialect.fulltext_ddl("session_messages", "content"):
        conn.execute(statement)


def _v8_session_history(conn: "Connection", dialect: "Dialect") -> None:
    """Add environment-scoped session history and its derived state tables.

    Preserve v7 rows in the `default` environment. No transcript or usage
    data is rewritten or deleted. The legacy writers continue to work through
    column defaults while the server read/write cutover is implemented later.
    """
    types = dialect.type_map()
    additions = {
        "sessions": (
            "environment_id", "owner_user_id", "visibility",
            "prompt_snapshot_id", "next_seq", "next_turn_seq",
            "context_revision", "active_run_id", "lease_owner",
            "lease_until", "lease_epoch",
        ),
        "session_messages": ("environment_id", "run_id", "input_ordinal"),
        "session_usage": ("environment_id", "run_id"),
    }
    for table_name, column_names in additions.items():
        table = next(t for t in sch.SESSION_TABLES if t.name == table_name)
        existing = set(dialect.column_names(conn, table_name))
        for column in table.columns:
            if column.name not in column_names or column.name in existing:
                continue
            suffix = f" {column.constraints}" if column.constraints else ""
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN "
                f"{column.name} {types[column.type]}{suffix}"
            )
    conn.execute("UPDATE sessions SET owner_user_id = user_id WHERE owner_user_id IS NULL")

    for child in ("session_messages", "session_usage"):
        mismatch = conn.fetchone(
            f"SELECT 1 AS found FROM {child} c LEFT JOIN sessions s "
            "ON s.id = c.session_id "
            "WHERE s.id IS NULL OR s.tenant_id <> c.tenant_id "
            "OR s.environment_id <> c.environment_id LIMIT 1"
        )
        if mismatch:
            raise ValueError(f"{child} has rows outside their parent session scope")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_scope_id "
        "ON sessions(tenant_id, environment_id, id)"
    )
    for child in ("session_messages", "session_usage"):
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{child}_order "
            f"ON {child}(tenant_id, environment_id, session_id, seq)"
        )
    conn.executescript(sch.render((
        sch.SESSION_ROUTES, sch.SESSION_CONTEXTS,
        sch.SESSION_PROMPT_SNAPSHOTS, sch.SESSION_PARTICIPANTS,
    ), dialect))


def _v9_tokenomics_dimensions(conn: "Connection", dialect: "Dialect") -> None:
    """Add report dimensions for group conversations and org user attributes."""
    types = dialect.type_map()
    sessions = set(dialect.column_names(conn, "sessions"))
    if "group_id" not in sessions:
        conn.execute(
            f"ALTER TABLE sessions ADD COLUMN group_id {types['TEXT']}"
        )
    conn.executescript(sch.render((
        sch.CONVERSATION_GROUPS,
        sch.AUTH_MEMBERSHIP_ATTRIBUTES,
    ), dialect))


def _v10_local_credentials(conn: "Connection", dialect: "Dialect") -> None:
    conn.executescript(sch.render((sch.AUTH_LOCAL_CREDENTIALS,), dialect))


def _v11_user_groups(conn: "Connection", dialect: "Dialect") -> None:
    conn.executescript(sch.render((sch.AUTH_GROUPS, sch.AUTH_GROUP_MEMBERSHIPS), dialect))


def _v12_graph_definitions(conn: "Connection", dialect: "Dialect") -> None:
    conn.executescript(sch.render(sch.GRAPH_TABLES, dialect))
    columns = set(dialect.column_names(conn, "flow_runs"))
    if not columns:
        return
    types = dialect.type_map()
    if "definition_id" not in columns:
        conn.execute(f"ALTER TABLE flow_runs ADD COLUMN definition_id {types['TEXT']}")
    if "definition_version" not in columns:
        conn.execute(f"ALTER TABLE flow_runs ADD COLUMN definition_version {types['INTEGER']}")


def _v13_prefixed_tables(conn: "Connection", dialect: "Dialect") -> None:
    """Rename the v12 tables in place, preserving rows, indexes and FKs."""
    for table in sch.ALL_TABLES:
        old = table.name
        prefix = "ai_" if old == "sessions" or old.startswith("session_") \
            or old == "conversation_groups" else "fnd_"
        new = prefix + old
        if dialect.column_names(conn, old):
            if dialect.column_names(conn, new):
                raise RuntimeError(f"Both old and new names exist for {old}")
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    conn.executescript(current_schema.render((current_schema.AI_STATE_META,), dialect))
    conn.execute(f"ALTER TABLE {VERSION_TABLE} RENAME TO {CURRENT_VERSION_TABLE}")


def _v14_mail_tables(conn: "Connection", dialect: "Dialect") -> None:
    conn.executescript(current_schema.render(current_schema.MAIL_TABLES, dialect))


def _v15_remove_unprefixed_version_artifact(conn: "Connection", dialect: "Dialect") -> None:
    if not dialect.column_names(conn, VERSION_TABLE):
        return
    row = conn.fetchone(f"SELECT COUNT(*) AS n FROM {VERSION_TABLE}")
    if row and int(row["n"]):
        raise RuntimeError("Unprefixed flow schema history is non-empty; refusing to drop it")
    conn.execute(f"DROP TABLE {VERSION_TABLE}")


def _v16_migration_conflict_archive(conn: "Connection", dialect: "Dialect") -> None:
    conn.executescript(current_schema.render(current_schema.MIGRATION_TABLES, dialect))


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline: flow runs, states, edges, events, deployments", _v1_baseline),
    Migration(2, "flow_run_states keyed on (run_id, seq); drop unused id", _v2_run_states_natural_key),
    Migration(3, "flow_runs.owner_id + heartbeat_at for multi-writer reconciliation", _v3_run_ownership),
    Migration(4, "user_id + tenant_id on runs, events and deployments", _v4_tenancy),
    Migration(5, "identity store: orgs, users, memberships, api keys", _v5_identity),
    Migration(6, "navigation: pages, functions, menus, items, roles, versions", _v6_navigation),
    Migration(7, "sessions, session_messages, session_usage (per-call tokens)", _v7_sessions),
    Migration(8, "environment-scoped session history, routes and contexts", _v8_session_history),
    Migration(9, "tokenomics report group and organization dimensions", _v9_tokenomics_dimensions),
    Migration(10, "local dashboard password credentials", _v10_local_credentials),
    Migration(11, "tenant-scoped user groups and memberships", _v11_user_groups),
    Migration(12, "versioned database-backed flow graphs", _v12_graph_definitions),
    Migration(13, "prefix AI and foundation tables", _v13_prefixed_tables),
    Migration(14, "PostgreSQL mail sample tables", _v14_mail_tables),
    Migration(15, "remove empty unprefixed schema version artifact", _v15_remove_unprefixed_version_artifact),
    Migration(16, "archive source rows that conflict with active PostgreSQL identities", _v16_migration_conflict_archive),
)

SCHEMA_VERSION = MIGRATIONS[-1].version


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer gyrfalcon than this one."""


def _refuse_if_newer(version: int) -> None:
    if version > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"The flow database is at schema v{version}, but this gyrfalcon "
            f"understands v{SCHEMA_VERSION}. An older process must not write "
            f"to a newer schema — upgrade gyrfalcon, or point flow.store at a "
            f"different database."
        )


def _ensure_version_table(conn: "Connection", dialect: "Dialect") -> None:
    types = dialect.type_map()
    table = CURRENT_VERSION_TABLE if dialect.column_names(conn, CURRENT_VERSION_TABLE) else VERSION_TABLE
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        f"  version {types['INTEGER']} PRIMARY KEY,"
        f"  description {types['TEXT']},"
        f"  applied_at {types['REAL']} NOT NULL"
        f")"
    )


def current_version(conn: "Connection", dialect: "Dialect") -> int:
    """Highest applied migration, 0 for an unversioned or empty database.

    Strictly read-only. It used to create the version table on the way past,
    which put DDL on the path every process takes at startup — and
    `CREATE TABLE IF NOT EXISTS` is *not* concurrency-safe on PostgreSQL: the
    existence check and the creation are not atomic, so simultaneous callers
    collide on `pg_type` with a duplicate-key error. Creation now happens only
    under the migration lock.
    """
    table = CURRENT_VERSION_TABLE if dialect.column_names(conn, CURRENT_VERSION_TABLE) else VERSION_TABLE
    if not dialect.column_names(conn, table):
        return 0
    row = conn.fetchone(f"SELECT COALESCE(MAX(version), 0) AS v FROM {table}")
    return int(row["v"]) if row else 0


def ensure_schema(db: "Database") -> int:
    """Bring the database up to `SCHEMA_VERSION`. Returns the version applied.

    Idempotent and safe to call from every store constructor — after the first
    call it is a single SELECT.
    """
    dialect = db.dialect

    # Fast path, taken by every process on every startup: read-only, so it
    # neither writes nor contends.
    with db.connect() as conn:
        version = current_version(conn, dialect)
    _refuse_if_newer(version)
    if version == SCHEMA_VERSION:
        return version

    # The lock wraps the whole read-apply-record sequence, and the version is
    # re-read inside it: a peer may have finished migrating while this process
    # queued for the lock. All DDL lives in here.
    with dialect.migration_lock(db):
        with db.connect() as conn:
            _ensure_version_table(conn, dialect)
            version = current_version(conn, dialect)
        _refuse_if_newer(version)

        for migration in MIGRATIONS:
            if migration.version <= version:
                continue
            # One transaction per migration, so a failure leaves the database
            # at the previous version rather than half-migrated.
            with db.connect() as conn:
                migration.apply(conn, dialect)
                version_table = CURRENT_VERSION_TABLE if migration.version >= 13 else VERSION_TABLE
                conn.execute(
                    f"INSERT INTO {version_table} (version, description, applied_at) "
                    f"VALUES (?,?,?)",
                    (migration.version, migration.description, time.time()),
                )

    return SCHEMA_VERSION
