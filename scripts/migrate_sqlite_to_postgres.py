"""One-time, non-destructive import of the former profile databases.

Run after backing up PostgreSQL and taking consistent SQLite snapshots.
The source files are opened read-only. Existing PostgreSQL identity wins when
the same login has diverged; source files remain the recovery record.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path

from gyrfalcon.db import legacy_schema, open_database
from gyrfalcon.db.migrations import ensure_schema


def rows(source: sqlite3.Connection, table: str):
    return source.execute(f"SELECT * FROM {table}").fetchall()


def archive_conflict(conn, table: str, key: str, row) -> None:
    conn.execute(
        "INSERT INTO fnd_migration_conflicts "
        "(source, source_table, source_key, payload, reason, migrated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        ("flow.db", table, key, json.dumps(dict(row), sort_keys=True),
         "A PostgreSQL identity with the same natural key already exists", time.time()),
    )


def copy_flow(source: sqlite3.Connection, conn) -> None:
    user_map = {}
    for user in rows(source, "auth_users"):
        existing = conn.fetchone(
            "SELECT id FROM fnd_auth_users WHERE issuer = ? AND subject = ?",
            (user["issuer"], user["subject"]),
        )
        if existing:
            user_map[user["id"]] = existing["id"]
    for table in legacy_schema.ALL_TABLES:
        old = table.name
        new = ("ai_" if old == "sessions" or old.startswith("session_")
               or old == "conversation_groups" else "fnd_") + old
        source_rows = rows(source, old)
        if not source_rows:
            continue
        columns = [column.name for column in table.columns]
        statement = (f"INSERT INTO {new} ({', '.join(columns)}) "
                     f"VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT DO NOTHING")
        copied = 0
        for row in source_rows:
            if old in {"auth_users", "auth_local_credentials"} and row["id" if old == "auth_users" else "user_id"] in user_map:
                key = row["issuer"] + "|" + row["subject"] if old == "auth_users" else row["username"]
                archive_conflict(conn, old, key, row)
                continue  # PostgreSQL's existing login and password take precedence.
            values = {column: row[column] for column in columns}
            if old in {"auth_memberships", "auth_api_keys", "auth_group_memberships", "auth_membership_attributes"}:
                values["user_id"] = user_map.get(values["user_id"], values["user_id"])
            result = conn.execute(statement, tuple(values[column] for column in columns))
            copied += result.rowcount
            if result.rowcount == 0 and old in {"auth_orgs", "auth_memberships"}:
                key = row["id"] if old == "auth_orgs" else f"{row['user_id']}|{row['org_id']}"
                archive_conflict(conn, old, key, row)
        print(f"{old}: source={len(source_rows)} inserted={copied} existing_or_conflict={len(source_rows)-copied}")
    for old_user_id, active_user_id in user_map.items():
        if old_user_id != active_user_id:
            conn.execute("DELETE FROM fnd_auth_memberships WHERE user_id = ?", (old_user_id,))


def copy_legacy_sessions(source: sqlite3.Connection, conn) -> None:
    source_sessions = rows(source, "sessions")
    id_map = {}
    for row in source_sessions:
        original = row["id"]
        existing = conn.fetchone("SELECT id, started_at, system_prompt FROM ai_sessions WHERE id = ?", (original,))
        same_source = existing and existing["started_at"] == row["started_at"] \
            and existing["system_prompt"] == row["system_prompt"]
        id_map[original] = "legacy-" + original if existing and not same_source else original

    for row in source_sessions:
        session_id = id_map[row["id"]]
        parent = id_map.get(row["parent_session_id"], row["parent_session_id"])
        conn.execute(
            "INSERT INTO ai_sessions (id, source, agent_id, model, parent_session_id, title, "
            "system_prompt, started_at, ended_at, last_active, uncached_input_tokens, "
            "cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, "
            "cost_usd, user_id, tenant_id, owner_user_id) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
            (session_id, row["source"], row["agent_id"], row["model"], parent,
             row["title"], row["system_prompt"], row["started_at"], row["ended_at"],
             row["last_active"], row["input_tokens"] or 0, row["cache_read_tokens"] or 0,
             row["cache_write_tokens"] or 0, row["output_tokens"] or 0,
             row["reasoning_tokens"] or 0, row["cost"] or 0, "local", "local", "local"),
        )
    print(f"legacy sessions: source={len(source_sessions)} destination IDs verified later")

    messages = source.execute("SELECT * FROM messages ORDER BY session_id, id").fetchall()
    sequence: dict[str, int] = {}
    for row in messages:
        session_id = id_map[row["session_id"]]
        sequence[session_id] = sequence.get(session_id, 0) + 1
        message_id = uuid.uuid5(uuid.NAMESPACE_URL, f"gyrfalcon-legacy-message:{row['id']}").hex
        conn.execute(
            "INSERT INTO ai_session_messages (id, session_id, seq, role, content, tool_call_id, "
            "tool_calls, tool_name, reasoning, created_at, user_id, tenant_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
            (message_id, session_id, sequence[session_id], row["role"], row["content"],
             row["tool_call_id"], row["tool_calls"], row["tool_name"], row["reasoning"],
             row["created_at"], "local", "local"),
        )
    for session_id, count in sequence.items():
        conn.execute("UPDATE ai_sessions SET next_seq = GREATEST(next_seq, ?) WHERE id = ?", (count, session_id))
    # An earlier import could have created an empty prefixed duplicate after
    # the original ID was already imported. Remove only that proven duplicate.
    for original, mapped in id_map.items():
        duplicate = "legacy-" + original
        if mapped == original:
            count = conn.fetchone("SELECT COUNT(*) AS n FROM ai_session_messages WHERE session_id = ?", (duplicate,))
            if count["n"] == 0:
                conn.execute("DELETE FROM ai_sessions WHERE id = ?", (duplicate,))
    print(f"legacy messages: source={len(messages)}")

    meta = rows(source, "state_meta")
    for row in meta:
        conn.execute("INSERT INTO ai_state_meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO NOTHING", (row["key"], row["value"]))
    print(f"legacy state_meta: source={len(meta)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    args = parser.parse_args()
    db = open_database()
    try:
        ensure_schema(db)
        with sqlite3.connect(f"file:{args.flow}?mode=ro", uri=True) as flow, \
             sqlite3.connect(f"file:{args.sessions}?mode=ro", uri=True) as sessions:
            flow.row_factory = sqlite3.Row
            sessions.row_factory = sqlite3.Row
            with db.connect() as conn:
                copy_flow(flow, conn)
                copy_legacy_sessions(sessions, conn)
    finally:
        db.close()


if __name__ == "__main__":
    main()
