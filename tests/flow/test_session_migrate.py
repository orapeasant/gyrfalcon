"""Copying the legacy SQLite store into the shared database.

Spec: §19.7.
"""

import sqlite3

import pytest

from gyrfalcon.db.scope import Scope
from gyrfalcon.sessions.migrate import migrate
from gyrfalcon.sessions.store import SessionStore

ALICE = Scope(tenant_id="acme", user_id="alice")


@pytest.fixture()
def store(store_target):
    s = SessionStore(**store_target)
    yield s
    s.close()


@pytest.fixture()
def legacy(tmp_path):
    """A legacy store with two sessions and some messages."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, agent_id TEXT,
            model TEXT, parent_session_id TEXT, title TEXT, system_prompt TEXT,
            started_at REAL NOT NULL, ended_at REAL, last_active REAL,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0.0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
            tool_name TEXT, reasoning TEXT, created_at REAL NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO sessions (id, source, model, title, started_at, last_active) "
        "VALUES ('s1', 'cli', 'gpt-4o', 'First chat', 1000.0, 1001.0)")
    conn.execute(
        "INSERT INTO sessions (id, source, model, title, started_at, last_active) "
        "VALUES ('s2', 'web', 'claude-sonnet-4-5', 'Second', 2000.0, 2001.0)")
    for role, content in (("user", "hello there"), ("assistant", "hi back")):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES ('s1', ?, ?, 1000.5)", (role, content))
    conn.commit()
    conn.close()
    return path


class TestMigrate:
    def test_copies_sessions_and_messages(self, store, legacy):
        report = migrate(source=legacy, store=store, scope=ALICE)
        assert report.sessions_copied == 2
        assert report.messages_copied == 2
        assert report.errors == []

        row = store.get_session("s1", scope=ALICE)
        assert row["title"] == "First chat" and row["model"] == "gpt-4o"
        assert [m["content"] for m in store.get_messages("s1", scope=ALICE)] == [
            "hello there", "hi back"]

    def test_migrated_rows_belong_to_the_running_scope(self, store, legacy):
        migrate(source=legacy, store=store, scope=ALICE)
        row = store.get_session("s1", scope=ALICE)
        assert row["tenant_id"] == "acme" and row["user_id"] == "alice"

    def test_is_idempotent(self, store, legacy):
        migrate(source=legacy, store=store, scope=ALICE)
        again = migrate(source=legacy, store=store, scope=ALICE)
        assert again.sessions_copied == 0
        assert again.sessions_skipped == 2
        # And nothing was duplicated.
        assert len(store.get_messages("s1", scope=ALICE)) == 2

    def test_the_source_file_is_not_modified(self, store, legacy):
        before = legacy.read_bytes()
        migrate(source=legacy, store=store, scope=ALICE)
        assert legacy.read_bytes() == before

    def test_a_missing_source_reports_rather_than_raising(self, store, tmp_path):
        report = migrate(source=tmp_path / "nope.db", store=store, scope=ALICE)
        assert report.sessions_copied == 0
        assert report.errors

    def test_migrated_messages_are_searchable(self, store, legacy):
        """The copy must populate the full-text index, not just the table."""
        migrate(source=legacy, store=store, scope=ALICE)
        assert store.search_messages("hello", scope=ALICE)
