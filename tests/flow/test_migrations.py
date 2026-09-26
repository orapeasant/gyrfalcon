"""Schema migrations — §15.8.

`ensure_schema` is the one function every process calls at startup against a
database it may not own the history of. A bug here does not surface as "a
test would have caught it" — it surfaces as every install upgrading wrong at
once, silently, because `ALTER TABLE ADD COLUMN` and `CREATE TABLE IF NOT
EXISTS` both "succeed" against a wrong assumption.

These tests build the pre-migration shapes `migrations.py`'s own docstrings
describe (v1 baseline predating owner columns, v3 predating tenancy), load
them with real rows, and check what a trip to `SCHEMA_VERSION` actually
preserves — not just that no exception was raised.

Legacy DDL is written with `dialect.type_map()`, the same way `schema.py`
itself is, so one set of statements is meaningful on both backends — the same
property `tests/flow/test_scope.py` holds the rest of the package to.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym
from conftest import postgres_test_dsn

pytestmark = requires("gyrfalcon.db.migrations:ensure_schema", section="§15.8 migrations")


def _backend_params():
    dsn = postgres_test_dsn()
    skip = pytest.mark.skip(
        reason="PostgreSQL suite needs GYRFALCON_TEST_PG_DSN set and pg8000 installed"
    )
    return [
        pytest.param("postgres", id="postgres", marks=() if dsn else (skip,)),
    ]


@pytest.fixture(params=_backend_params())
def raw_db(request, tmp_path):
    """An empty database, tables and version-table alike — the pre-migration
    starting point. Not `store_target`: that fixture pre-creates the current
    schema, which is exactly what these tests must not start from.

    PostgreSQL has no per-test database (§conftest.store_target's own
    comment), so a test here that deliberately builds a *broken* legacy shape
    — or one whose migration is expected to fail and roll back — must not
    leave that shape sitting in the shared database for the next test (here
    or in any other file) to inherit. Dropped on the way in AND the way out.
    """
    open_database = sym("gyrfalcon.db:open_database")

    dsn = postgres_test_dsn()

    def _wipe() -> None:
        # A fresh connection, independent of whatever state a test left `db`
        # in (some tests close it themselves to reopen through RunStore).
        cleaner = open_database(backend="postgres", dsn=dsn)
        try:
            with cleaner.connect() as conn:
                from gyrfalcon.db import legacy_schema, schema

                names = {table.name for table in legacy_schema.ALL_TABLES}
                names.update(table.name for table in schema.ALL_TABLES)
                names.update(("flow_schema_version", "fnd_flow_schema_version"))
                for name in names:
                    conn.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        finally:
            cleaner.close()

    _wipe()
    db = open_database(backend="postgres", dsn=dsn)
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass
        _wipe()


def _create_legacy_v1(conn, dialect) -> None:
    """The shape `_v1_baseline` describes: no owner columns, no tenancy, and
    `flow_run_states` still carries its dead surrogate `id`."""
    t = dialect.type_map()
    conn.execute(f"""
        CREATE TABLE flow_runs (
            id {t['TEXT']} PRIMARY KEY,
            name {t['TEXT']} NOT NULL,
            kind {t['TEXT']} NOT NULL,
            state_type {t['TEXT']} NOT NULL,
            state_name {t['TEXT']},
            parameters {t['TEXT']},
            result {t['TEXT']},
            error {t['TEXT']},
            parent_run_id {t['TEXT']},
            flow_run_id {t['TEXT']},
            tags {t['TEXT']},
            retries {t['INTEGER']} DEFAULT 0,
            created_at {t['REAL']} NOT NULL,
            started_at {t['REAL']},
            updated_at {t['REAL']} NOT NULL,
            finished_at {t['REAL']}
        )
    """)
    conn.execute(f"""
        CREATE TABLE flow_run_states (
            id {t['INTEGER']},
            run_id {t['TEXT']} NOT NULL,
            seq {t['INTEGER']} NOT NULL,
            state_type {t['TEXT']} NOT NULL,
            state_name {t['TEXT']},
            message {t['TEXT']},
            state_details {t['TEXT']},
            orchestration {t['TEXT']},
            at {t['REAL']} NOT NULL,
            UNIQUE(run_id, seq)
        )
    """)
    conn.execute(f"""
        CREATE TABLE flow_run_edges (
            downstream {t['TEXT']} NOT NULL,
            upstream {t['TEXT']} NOT NULL,
            kind {t['TEXT']} NOT NULL DEFAULT 'data',
            PRIMARY KEY (downstream, upstream, kind)
        )
    """)
    conn.execute(f"""
        CREATE TABLE flow_events (
            id {t['TEXT']} PRIMARY KEY,
            occurred {t['REAL']} NOT NULL,
            event {t['TEXT']} NOT NULL,
            resource_id {t['TEXT']} NOT NULL,
            resource {t['TEXT']} NOT NULL,
            related {t['TEXT']} NOT NULL,
            payload {t['TEXT']} NOT NULL,
            follows {t['TEXT']}
        )
    """)
    conn.execute(f"""
        CREATE TABLE flow_deployments (
            id {t['TEXT']} PRIMARY KEY,
            name {t['TEXT']} NOT NULL UNIQUE,
            flow_name {t['TEXT']} NOT NULL,
            schedule_raw {t['TEXT']},
            schedule {t['TEXT']},
            parameters {t['TEXT']} NOT NULL DEFAULT '{{}}',
            tags {t['TEXT']} NOT NULL DEFAULT '[]',
            concurrency_limit {t['INTEGER']},
            enforce_parameter_schema {t['BOOL']} NOT NULL DEFAULT 0,
            paused {t['BOOL']} NOT NULL DEFAULT 0,
            next_run_at {t['TEXT']},
            created_at {t['REAL']} NOT NULL,
            updated_at {t['REAL']} NOT NULL
        )
    """)


def _seed_v1_row(conn) -> None:
    conn.execute(
        "INSERT INTO flow_runs (id, name, kind, state_type, state_name, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-run", "old-flow", "flow", "COMPLETED", "Completed", 1000.0, 1000.0),
    )
    conn.execute(
        "INSERT INTO flow_run_states (run_id, seq, state_type, state_name, at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("legacy-run", 1, "COMPLETED", "Completed", 1000.0),
    )
    conn.execute(
        "INSERT INTO flow_events (id, occurred, event, resource_id, resource, "
        "related, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("evt-1", 1000.0, "gyrfalcon.flow-run.Completed", "legacy-run",
         "{}", "[]", "{}"),
    )
    conn.execute(
        "INSERT INTO flow_deployments (id, name, flow_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dep-1", "legacy-dep", "old-flow", 1000.0, 1000.0),
    )
    conn.execute(
        "INSERT INTO flow_run_edges (downstream, upstream, kind) VALUES (?, ?, ?)",
        ("legacy-run", "legacy-run", "data"),
    )


class TestFreshDatabase:
    def test_a_new_database_lands_at_current_version_in_one_call(self, raw_db):
        ensure_schema = sym("gyrfalcon.db.migrations:ensure_schema")
        SCHEMA_VERSION = sym("gyrfalcon.db.migrations:SCHEMA_VERSION")
        assert ensure_schema(raw_db) == SCHEMA_VERSION

    def test_ensure_schema_is_idempotent(self, raw_db):
        ensure_schema = sym("gyrfalcon.db.migrations:ensure_schema")
        assert ensure_schema(raw_db) == ensure_schema(raw_db)


class TestLegacyUpgrade:
    """A real production database, not a synthetic one built for this suite.

    Any SQLite/PostgreSQL `flow.db` that predates the `owner_id`/`heartbeat_at`
    columns (i.e. every install that existed before multi-writer support
    landed) is completely unversioned — there is no `flow_schema_version`
    table, so `current_version()` reports 0 and `ensure_schema()` must run
    every migration starting from `_v1_baseline`.
    """

    def test_a_legacy_database_upgrades_without_touching_future_columns(self, raw_db):
        """`_v1_baseline` re-renders *every* current table via
        `sch.render(sch.ALL_TABLES, dialect)` — including
        `CREATE INDEX idx_runs_heartbeat ON flow_runs(heartbeat_at)`, an index
        that belongs to the *current* `schema.py`, which already carries the
        v3/v4 columns inline. `CREATE TABLE IF NOT EXISTS` silently no-ops
        against the pre-existing legacy `flow_runs` (correct), but the
        `CREATE INDEX` right after it is unconditional and fires against a
        table that does not yet have `heartbeat_at` — that column is only
        added three migrations later, by `_v3_run_ownership`.

        The result: `ensure_schema()` raises `OperationalError` /
        `DatabaseError` ("no such column: heartbeat_at" / column does not
        exist) on the very first migration, on both backends, for every
        database that has ever been used in production before this feature
        shipped. A fresh install never hits this because a fresh `flow_runs`
        already has the column by the time any index is created.
        """
        ensure_schema = sym("gyrfalcon.db.migrations:ensure_schema")
        with raw_db.connect() as conn:
            _create_legacy_v1(conn, raw_db.dialect)
            _seed_v1_row(conn)

        ensure_schema(raw_db)  # should reach SCHEMA_VERSION; instead raises


class TestV2ToCurrent:
    """A database already past the surrogate-key cleanup, but before ownership
    or tenancy — the shape most real installs are actually in today, and the
    one explicitly asked for: "a database at v2 upgrading to v4"."""

    @pytest.fixture()
    def v2(self, raw_db):
        migrations = sym("gyrfalcon.db.migrations")
        with raw_db.connect() as conn:
            _create_legacy_v1(conn, raw_db.dialect)
            # v2 already ran: flow_run_states lost its dead surrogate `id`.
            conn.execute("ALTER TABLE flow_run_states DROP COLUMN id")
            _seed_v1_row(conn)
            migrations._ensure_version_table(conn, raw_db.dialect)
            conn.execute(
                f"INSERT INTO {migrations.VERSION_TABLE} (version, description, applied_at) "
                f"VALUES (?, ?, ?)",
                (2, "stamped for test", 0.0),
            )
        return raw_db

    def test_upgrade_reaches_current_version(self, v2):
        migrations = sym("gyrfalcon.db.migrations")
        with v2.connect() as conn:
            assert migrations.current_version(conn, v2.dialect) == 2
        assert migrations.ensure_schema(v2) == migrations.SCHEMA_VERSION

    def test_no_rows_are_lost_across_any_owned_table(self, v2):
        migrations = sym("gyrfalcon.db.migrations")
        migrations.ensure_schema(v2)
        with v2.connect() as conn:
            for table in ("fnd_flow_runs", "fnd_flow_run_states", "fnd_flow_run_edges",
                          "fnd_flow_events", "fnd_flow_deployments"):
                n = conn.fetchone(f"SELECT COUNT(*) AS c FROM {table}")["c"]
                assert n == 1, f"{table} lost or gained rows across the upgrade"

    def test_content_and_tenancy_backfill_survive(self, v2):
        """§17.4: a row that predates tenancy is `local`'s, not nobody's."""
        migrations = sym("gyrfalcon.db.migrations")
        migrations.ensure_schema(v2)
        with v2.connect() as conn:
            run = conn.fetchone("SELECT * FROM fnd_flow_runs WHERE id = ?", ("legacy-run",))
            event = conn.fetchone("SELECT * FROM fnd_flow_events WHERE id = ?", ("evt-1",))
            dep = conn.fetchone("SELECT * FROM fnd_flow_deployments WHERE id = ?", ("dep-1",))
            state = conn.fetchone(
                "SELECT * FROM fnd_flow_run_states WHERE run_id = ? AND seq = ?",
                ("legacy-run", 1),
            )
        assert run["name"] == "old-flow" and run["state_type"] == "COMPLETED"
        assert state["state_name"] == "Completed"
        for row in (run, event, dep):
            assert row["user_id"] == "local"
            assert row["tenant_id"] == "local"
        # v3's columns are new here too, and must be null for a terminal run.
        assert run["owner_id"] is None
        assert run["heartbeat_at"] is None

    def test_the_upgraded_store_is_immediately_usable(self, v2):
        """Not just "columns exist" — a real RunStore must be able to open and
        query the upgraded database without a second migration pass."""
        migrations = sym("gyrfalcon.db.migrations")
        migrations.ensure_schema(v2)

        RunStore = sym("gyrfalcon.flow.store:RunStore")
        ident = sym("gyrfalcon.identity")

        v2.close()  # RunStore opens its own handle on the same path/dsn
        store = RunStore(dsn=postgres_test_dsn(), reconcile=False, emit_events=False)
        try:
            with ident.use_principal(ident.Principal(user_id="local", tenant_id="local")):
                row = store.get_run("legacy-run")
            assert row is not None
            assert row["name"] == "old-flow"
        finally:
            store.close()


class TestPartialUpgrades:
    """A database that already advanced partway — the common real case, since
    every process that has ever connected already ran *some* migrations."""

    def test_a_v3_shaped_database_only_needs_tenancy_added(self, raw_db):
        """owner_id/heartbeat_at already present; only v4's columns are new."""
        migrations = sym("gyrfalcon.db.migrations")
        with raw_db.connect() as conn:
            _create_legacy_v1(conn, raw_db.dialect)
            # Promote to the v3 shape by hand, mirroring what _v3_run_ownership
            # itself would add.
            t = raw_db.dialect.type_map()
            conn.execute(f"ALTER TABLE flow_runs ADD COLUMN owner_id {t['TEXT']}")
            conn.execute(f"ALTER TABLE flow_runs ADD COLUMN heartbeat_at {t['REAL']}")
            _seed_v1_row(conn)
            migrations._ensure_version_table(conn, raw_db.dialect)
            conn.execute(
                f"INSERT INTO {migrations.VERSION_TABLE} (version, description, applied_at) "
                f"VALUES (?, ?, ?)",
                (3, "stamped for test", 0.0),
            )

        with raw_db.connect() as conn:
            assert migrations.current_version(conn, raw_db.dialect) == 3
        assert migrations.ensure_schema(raw_db) == migrations.SCHEMA_VERSION

        with raw_db.connect() as conn:
            columns = raw_db.dialect.column_names(conn, "fnd_flow_runs")
            row = conn.fetchone("SELECT * FROM fnd_flow_runs WHERE id = ?", ("legacy-run",))
        assert "tenant_id" in columns and "user_id" in columns
        assert row["tenant_id"] == "local"
        # v3's own columns must not have been rebuilt or lost in the process.
        assert row["owner_id"] is None

    def test_only_some_tables_pre_exist(self, raw_db):
        """A database with just `flow_runs`, and nothing else. Only `flow_runs`
        pre-exists here, already at the *current* (v4) column shape —
        deliberately, so this test isolates "some tables missing" from the
        separate, known-broken "a legacy-shaped table already exists" path
        covered by `TestKnownBug`. The missing tables must be created at the
        current shape, and the pre-existing row must survive untouched."""
        schema = sym("gyrfalcon.db.legacy_schema")
        with raw_db.connect() as conn:
            conn.executescript(schema.render((schema.FLOW_RUNS,), raw_db.dialect))
            conn.execute(
                "INSERT INTO flow_runs (id, name, kind, state_type, created_at, "
                "updated_at, user_id, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("solo", "solo-flow", "flow", "PENDING", 1.0, 1.0, "alice", "acme"),
            )

        ensure_schema = sym("gyrfalcon.db.migrations:ensure_schema")
        SCHEMA_VERSION = sym("gyrfalcon.db.migrations:SCHEMA_VERSION")
        assert ensure_schema(raw_db) == SCHEMA_VERSION

        with raw_db.connect() as conn:
            for table in ("fnd_flow_runs", "fnd_flow_run_states", "fnd_flow_run_edges",
                          "fnd_flow_events", "fnd_flow_deployments"):
                assert raw_db.dialect.column_names(conn, table), f"{table} was not created"
            run = conn.fetchone("SELECT * FROM fnd_flow_runs WHERE id = ?", ("solo",))
            events_cols = raw_db.dialect.column_names(conn, "fnd_flow_events")
        assert run["tenant_id"] == "acme", "a real (non-backfilled) tenant must survive untouched"
        assert "tenant_id" in events_cols, "a freshly created table must already be current-shaped"


class TestRefusesNewerSchemas:
    def test_a_schema_from_the_future_is_refused_not_silently_written_to(self, raw_db):
        migrations = sym("gyrfalcon.db.migrations")
        ensure_schema = migrations.ensure_schema
        ensure_schema(raw_db)  # bring it to current, so the version table exists

        with raw_db.connect() as conn:
            conn.execute(
                f"INSERT INTO {migrations.CURRENT_VERSION_TABLE} (version, description, applied_at) "
                f"VALUES (?, ?, ?)",
                (migrations.SCHEMA_VERSION + 1, "from the future", 0.0),
            )

        with pytest.raises(migrations.SchemaTooNewError):
            ensure_schema(raw_db)

    def test_refusal_happens_before_any_migration_runs(self, raw_db):
        """An older process must not partially apply migrations it does
        understand before discovering the schema is ahead of it."""
        migrations = sym("gyrfalcon.db.migrations")
        with raw_db.connect() as conn:
            _create_legacy_v1(conn, raw_db.dialect)
            _seed_v1_row(conn)
            migrations._ensure_version_table(conn, raw_db.dialect)
            conn.execute(
                f"INSERT INTO {migrations.VERSION_TABLE} (version, description, applied_at) "
                f"VALUES (?, ?, ?)",
                (migrations.SCHEMA_VERSION + 5, "from the future", 0.0),
            )

        with pytest.raises(migrations.SchemaTooNewError):
            migrations.ensure_schema(raw_db)

        with raw_db.connect() as conn:
            columns = raw_db.dialect.column_names(conn, "flow_runs")
        assert "tenant_id" not in columns, (
            "a refused upgrade must not have touched the schema at all"
        )
