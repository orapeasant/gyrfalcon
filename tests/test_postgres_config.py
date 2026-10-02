"""The application uses PostgreSQL for persistent storage."""

from __future__ import annotations

import pytest


def test_postgres_is_the_storage_backend(monkeypatch):
    from gyrfalcon.db import resolve_target, store_settings

    monkeypatch.setenv("GYRFALCON_DB_BACKEND", "postgres")
    monkeypatch.setenv("GYRFALCON_DB_DSN", "postgresql://test:test@localhost/test")
    assert store_settings().backend == "postgres"
    assert resolve_target() == ("postgres", None, "postgresql://test:test@localhost/test")


def test_non_postgres_backend_is_rejected(monkeypatch):
    from gyrfalcon.db import resolve_target, store_settings

    monkeypatch.setenv("GYRFALCON_DB_BACKEND", "other")
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
