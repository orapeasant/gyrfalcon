"""Alembic entry point for the shared PostgreSQL schema."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from gyrfalcon.db.schema import metadata

BASELINE_REVISION = "0001_baseline"
_PRE_ALEMBIC_ADDITIONS = frozenset({"fnd_flow_attrs"})
_POST_BASELINE_VISUAL_TABLES = frozenset({
    "fnd_flow_dt_definitions", "fnd_flow_dt_definition_versions",
    "fnd_flow_dt_attrs", "fnd_flow_dt_notif", "fnd_flow_dt_deployments",
    "fnd_flow_rt_statuses", "fnd_flow_rt_node_statuses",
    "fnd_flow_rt_events", "fnd_flow_rt_attrs", "fnd_flow_rt_notif",
})
_POST_BASELINE_MESSAGE_COLUMNS = frozenset({
    "flow_node_status_id", "message_kind", "channel", "direction", "subject",
    "sender", "recipients", "body_html", "external_message_id", "in_reply_to",
    "headers", "attachment_refs",
})
_POST_BASELINE_MESSAGE_INDEXES = frozenset({
    "idx_session_messages_flow_node", "uq_session_messages_scope_id",
})
_POST_BASELINE_SESSION_COLUMNS = frozenset({
    "flow_run_status_id", "flow_node_status_id", "run_status", "run_error",
})
_POST_BASELINE_SESSION_INDEXES = frozenset({
    "idx_sessions_flow_run", "uq_sessions_flow_node", "idx_sessions_run_status",
})
_POST_BASELINE_FLOW_DEFINITION_COLUMNS = frozenset({"enabled", "published"})


def _config(connection=None) -> Config:
    from pathlib import Path

    config = Config(str(Path(__file__).parent / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://")
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def ensure_schema(db) -> str:
    """Create or upgrade the current schema under a PostgreSQL advisory lock.

    A database with the current application tables but no Alembic revision
    can be adopted after its columns and indexes are checked. This does not
    replay the retired migration history.
    """
    with db.migration_lock() as connection:
        _adopt_current_schema(connection)
        command.upgrade(_config(connection), "head")
        return MigrationContext.configure(connection).get_current_revision() or ""


def _adopt_current_schema(connection) -> None:
    """Stamp an unversioned current schema, adding only new whole tables."""
    if MigrationContext.configure(connection).get_current_revision() is not None:
        return

    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    expected = set(metadata.tables)
    present = existing & expected
    if not present:
        return  # empty application schema: Alembic creates the baseline

    missing_tables = expected - existing
    unsupported = missing_tables - _PRE_ALEMBIC_ADDITIONS - _POST_BASELINE_VISUAL_TABLES
    if unsupported:
        raise RuntimeError(
            "Cannot adopt an older PostgreSQL schema; missing tables: "
            + ", ".join(sorted(unsupported))
        )

    for name in sorted(present):
        expected_columns = set(metadata.tables[name].columns.keys())
        existing_columns = {column["name"] for column in inspector.get_columns(name)}
        missing_columns = expected_columns - existing_columns
        if name == "ai_session_messages":
            missing_columns -= _POST_BASELINE_MESSAGE_COLUMNS
        elif name == "ai_sessions":
            missing_columns -= _POST_BASELINE_SESSION_COLUMNS
        elif name == "fnd_flow_dt_definitions":
            missing_columns -= _POST_BASELINE_FLOW_DEFINITION_COLUMNS
        if missing_columns:
            raise RuntimeError(
                f"Cannot adopt {name}; missing columns: {', '.join(sorted(missing_columns))}"
            )

        expected_indexes = {index.name for index in metadata.tables[name].indexes}
        existing_indexes = {index["name"] for index in inspector.get_indexes(name)}
        missing_indexes = expected_indexes - existing_indexes
        if name == "ai_session_messages":
            missing_indexes -= _POST_BASELINE_MESSAGE_INDEXES
        elif name == "ai_sessions":
            missing_indexes -= _POST_BASELINE_SESSION_INDEXES
        if missing_indexes:
            raise RuntimeError(
                f"Cannot adopt {name}; missing indexes: {', '.join(sorted(missing_indexes))}"
            )

    baseline_additions = missing_tables & _PRE_ALEMBIC_ADDITIONS
    if baseline_additions:
        metadata.create_all(
            connection,
            tables=[metadata.tables[name] for name in sorted(baseline_additions)],
            checkfirst=True,
        )
    command.stamp(_config(connection), BASELINE_REVISION)
