"""Shared fixtures for the flow-engine acceptance suite.

Written against docs/spec/gyrfalcon/15-flow.md before the engine exists; see
_spec.py for the skip-until-implemented mechanism.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# ── Recording helpers ─────────────────────────────────────────────────────────

class CallRecorder:
    """Records ordered calls. Used to assert rule nesting and hook ordering."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def record(self, label: str, payload: Any = None) -> None:
        self.calls.append((label, payload))

    @property
    def labels(self) -> list[str]:
        return [c[0] for c in self.calls]

    def clear(self) -> None:
        self.calls.clear()


@pytest.fixture()
def recorder() -> CallRecorder:
    return CallRecorder()


class FlakyCallable:
    """Fails `fail_times` times, then succeeds. For retry/backoff assertions."""

    def __init__(self, fail_times: int, exc: BaseException | None = None, result: Any = "ok"):
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("induced failure")
        self.result = result
        self.attempts = 0

    def __call__(self, *args, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.exc
        return self.result


@pytest.fixture()
def flaky():
    return FlakyCallable


class FakeClock:
    """Deterministic clock so scheduled_time assertions never race real time."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now_value = start

    def now(self) -> float:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


# ── Backend parameterization (§15.9) ──────────────────────────────────────────
#
# The store suite runs against every configured backend. SQLite always runs and
# needs nothing. PostgreSQL runs only when GYRFALCON_TEST_PG_DSN is set and
# pg8000 is installed — otherwise those parameters skip with a message saying
# how to enable them, rather than silently reporting green on half the matrix.
#
#   GYRFALCON_TEST_PG_DSN=postgresql://user:pass@host:5432/db uv run pytest

def postgres_test_dsn() -> str | None:
    dsn = os.environ.get("GYRFALCON_TEST_PG_DSN", "").strip()
    if not dsn:
        return None
    try:
        import pg8000.dbapi  # noqa: F401
    except ImportError:
        return None
    return dsn


def _backend_params():
    dsn = postgres_test_dsn()
    skip = pytest.mark.skip(
        reason="PostgreSQL suite needs GYRFALCON_TEST_PG_DSN set and pg8000 installed"
    )
    return [
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=() if dsn else (skip,)),
    ]


@pytest.fixture(params=_backend_params())
def store_target(request, tmp_path) -> dict:
    """Constructor kwargs pinning a store to the parameterized backend.

    SQLite gets a fresh file per test from `tmp_path`. PostgreSQL has no such
    per-test isolation — one database is shared — so the flow tables are
    truncated on entry. Truncating rather than dropping keeps the schema (and
    any migration state) intact between tests.
    """
    if request.param == "sqlite":
        return {"db_path": tmp_path / "flow.db"}

    dsn = postgres_test_dsn()
    from gyrfalcon.db import open_database
    from gyrfalcon.db import schema as sch

    db = open_database(backend="postgres", dsn=dsn)
    try:
        with db.connect() as conn:
            conn.executescript(sch.render(sch.ALL_TABLES, db.dialect))
            names = ", ".join(t.name for t in sch.ALL_TABLES)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    finally:
        db.close()
    return {"dsn": dsn}


@pytest.fixture()
def make_store(store_target):
    """Factory for stores on the current backend; closes them at teardown."""
    from gyrfalcon.flow.store import RunStore

    created = []

    def _make(**kwargs):
        s = RunStore(**{**store_target, **kwargs})
        created.append(s)
        return s

    yield _make
    for s in created:
        try:
            s.close()
        except Exception:
            pass
