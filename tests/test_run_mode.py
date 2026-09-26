"""Run mode does not change the PostgreSQL-only storage contract."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("mode", ["CLIENT", "SERVER"])
def test_both_modes_use_postgres(monkeypatch, mode):
    from gyrfalcon.db import resolve_target, store_settings

    monkeypatch.setenv("RUN_MODE", mode)
    monkeypatch.setenv("GYRFALCON_DB_BACKEND", "postgres")
    monkeypatch.setenv("GYRFALCON_DB_DSN", "postgresql://test:test@localhost/test")
    assert store_settings().backend == "postgres"
    assert resolve_target() == ("postgres", None, "postgresql://test:test@localhost/test")


def test_sqlite_is_rejected(monkeypatch):
    from gyrfalcon.db import resolve_target, store_settings

    monkeypatch.setenv("GYRFALCON_DB_BACKEND", "sqlite")
    with pytest.raises(ValueError, match="only supported"):
        store_settings()
    with pytest.raises(ValueError, match="File-backed"):
        resolve_target(db_path="flow.db")


def test_postgres_requires_dsn(monkeypatch, tmp_path):
    from gyrfalcon import gyrfalcon_constants
    from gyrfalcon.db import resolve_target

    monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
    monkeypatch.delenv("GYRFALCON_DB_DSN", raising=False)
    gyrfalcon_constants.get_gyrfalcon_home.cache_clear()
    try:
        with pytest.raises(ValueError, match="DSN is required"):
            resolve_target()
    finally:
        gyrfalcon_constants.get_gyrfalcon_home.cache_clear()
