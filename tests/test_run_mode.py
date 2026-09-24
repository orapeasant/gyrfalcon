"""RUN_MODE — SERVER vs CLIENT, and what it does to database selection.

Two rules carry this feature:

* `.env` wins over `--server`/`--client`. The CLI flag only fills in a value
  `.env` left unset, so a deployment cannot be talked out of its configured
  mode by a stray command-line argument.
* CLIENT is SQLite, always. Not "SQLite by default" — a CLIENT install
  ignores a configured PostgreSQL backend outright, because single-user is
  what CLIENT *means*, and making that structural is the difference between a
  rule and a convention someone forgets.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolate Gyrfalcon Home so config.yaml and flow.db are this test's own."""
    from gyrfalcon import gyrfalcon_constants as gc

    monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
    gc.get_gyrfalcon_home.cache_clear()
    yield tmp_path
    gc.get_gyrfalcon_home.cache_clear()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No inherited RUN_MODE / DB vars leaking in from the developer's shell."""
    for var in ("RUN_MODE", "GYRFALCON_DB_BACKEND", "GYRFALCON_DB_DSN"):
        monkeypatch.delenv(var, raising=False)


class TestGetRunMode:
    def test_unset_defaults_to_client(self):
        """The historical, zero-configuration behaviour: single-user, SQLite.
        Anyone who never sets RUN_MODE must see no change at all."""
        from gyrfalcon.gyrfalcon_constants import get_run_mode

        assert get_run_mode() == "CLIENT"

    @pytest.mark.parametrize("value", ["SERVER", "server", "  Server  "])
    def test_server_is_recognised_case_and_space_insensitively(self, value, monkeypatch):
        from gyrfalcon.gyrfalcon_constants import get_run_mode

        monkeypatch.setenv("RUN_MODE", value)
        assert get_run_mode() == "SERVER"

    @pytest.mark.parametrize("value", ["CLIENT", "client", "\tclient\n"])
    def test_client_is_recognised_case_and_space_insensitively(self, value, monkeypatch):
        from gyrfalcon.gyrfalcon_constants import get_run_mode

        monkeypatch.setenv("RUN_MODE", value)
        assert get_run_mode() == "CLIENT"

    @pytest.mark.parametrize("value", ["", "SERVERS", "cluster", "1", "true"])
    def test_an_unrecognised_value_falls_back_to_client(self, value, monkeypatch):
        """Falls back to the *narrower* mode. A typo must not silently promote
        an install to SERVER and start pointing it at a shared database."""
        from gyrfalcon.gyrfalcon_constants import get_run_mode

        monkeypatch.setenv("RUN_MODE", value)
        assert get_run_mode() == "CLIENT"


class TestCliFlagPrecedence:
    """`.env` wins; the flag is a fallback, not an override."""

    def test_the_flag_applies_when_env_is_unset(self, monkeypatch):
        from gyrfalcon.gyrfalcon_constants import get_run_mode
        from gyrfalcon_cli.main import _apply_run_mode

        _apply_run_mode("SERVER")

        assert get_run_mode() == "SERVER"

    def test_env_beats_the_flag(self, monkeypatch):
        from gyrfalcon.gyrfalcon_constants import get_run_mode
        from gyrfalcon_cli.main import _apply_run_mode

        monkeypatch.setenv("RUN_MODE", "CLIENT")
        _apply_run_mode("SERVER")   # --server on the command line

        assert get_run_mode() == "CLIENT", "a .env value must ignore the CLI flag"

    def test_env_beats_the_flag_in_the_other_direction_too(self, monkeypatch):
        from gyrfalcon.gyrfalcon_constants import get_run_mode
        from gyrfalcon_cli.main import _apply_run_mode

        monkeypatch.setenv("RUN_MODE", "SERVER")
        _apply_run_mode("CLIENT")

        assert get_run_mode() == "SERVER"

    def test_no_flag_and_no_env_leaves_it_unset(self, monkeypatch):
        """`_apply_run_mode(None)` is the "neither was given" path — it must not
        write a value, so `get_run_mode()` keeps owning the default."""
        import os

        from gyrfalcon_cli.main import _apply_run_mode

        _apply_run_mode(None)

        assert "RUN_MODE" not in os.environ


class TestDatabaseSelection:
    def test_client_uses_sqlite(self, home):
        from gyrfalcon.db import store_settings

        assert store_settings().backend == "sqlite"

    def test_client_ignores_a_configured_postgres_backend(self, home, monkeypatch):
        """The load-bearing rule. A CLIENT install pointed at PostgreSQL by a
        stray env var still opens its own local SQLite file."""
        from gyrfalcon.db import store_settings

        monkeypatch.setenv("RUN_MODE", "CLIENT")
        monkeypatch.setenv("GYRFALCON_DB_BACKEND", "postgres")
        monkeypatch.setenv("GYRFALCON_DB_DSN", "postgresql://user:pass@db:5432/gyrfalcon")

        settings = store_settings()
        assert settings.backend == "sqlite"
        assert settings.dsn == "", "a CLIENT must not even carry the DSN around"

    def test_server_honours_the_env_backend(self, home, monkeypatch):
        from gyrfalcon.db import store_settings

        monkeypatch.setenv("RUN_MODE", "SERVER")
        monkeypatch.setenv("GYRFALCON_DB_BACKEND", "postgres")
        monkeypatch.setenv("GYRFALCON_DB_DSN", "postgresql://user:pass@db:5432/gyrfalcon")

        settings = store_settings()
        assert settings.backend == "postgres"
        assert settings.dsn == "postgresql://user:pass@db:5432/gyrfalcon"

    def test_server_without_configuration_is_still_sqlite(self, home, monkeypatch):
        """SERVER changes what *can* be configured, not the default. An
        operator who flips the mode without setting a DSN gets a working
        install, not a startup failure."""
        from gyrfalcon.db import store_settings

        monkeypatch.setenv("RUN_MODE", "SERVER")

        assert store_settings().backend == "sqlite"

    def test_the_env_backend_beats_the_baked_in_config_default(self, home, monkeypatch):
        """DEFAULT_CONFIG always materialises `flow.store.backend: sqlite`, so
        a config-before-env precedence would make the env pair permanently
        unreachable — which is the bug this ordering exists to avoid."""
        from gyrfalcon.config import cfg_get
        from gyrfalcon.db import store_settings

        assert cfg_get("flow.store.backend", "") == "sqlite", "precondition"

        monkeypatch.setenv("RUN_MODE", "SERVER")
        monkeypatch.setenv("GYRFALCON_DB_BACKEND", "postgres")
        monkeypatch.setenv("GYRFALCON_DB_DSN", "postgresql://x/y")

        assert store_settings().backend == "postgres"

    @pytest.mark.parametrize("alias,expected", [
        ("postgresql", "postgres"), ("pg", "postgres"), ("sqlite3", "sqlite"),
    ])
    def test_backend_aliases_normalise(self, home, monkeypatch, alias, expected):
        from gyrfalcon.db import store_settings

        monkeypatch.setenv("RUN_MODE", "SERVER")
        monkeypatch.setenv("GYRFALCON_DB_BACKEND", alias)
        monkeypatch.setenv("GYRFALCON_DB_DSN", "postgresql://x/y")

        assert store_settings().backend == expected
