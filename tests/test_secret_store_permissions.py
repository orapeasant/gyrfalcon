"""The secret store holds values other systems accept as-is (a Slack bot token is
write access to a workspace), so it must not be readable by other local users."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from gyrfalcon import security
from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_secrets_store_file

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
    get_gyrfalcon_home.cache_clear()
    yield tmp_path
    get_gyrfalcon_home.cache_clear()


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_a_new_secret_file_is_owner_only():
    old = os.umask(0o022)  # the common default, which would have made it 0644
    try:
        security.create_secret("slack_bot_token", "bot", "xoxb-not-real-1234567890")
    finally:
        os.umask(old)
    assert _mode(get_secrets_store_file()) == 0o600


def test_an_existing_world_readable_file_is_tightened_on_the_next_write():
    path = get_secrets_store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"secrets": []}')
    os.chmod(path, 0o644)
    security.create_secret("k", "d", "v" * 12)
    assert _mode(path) == 0o600


def test_updates_and_deletes_keep_it_private():
    s = security.create_secret("k", "d", "value-value-1")
    security.update_secret(s["id"], value="another-value-1")
    assert _mode(get_secrets_store_file()) == 0o600
    security.delete_secret(s["id"])
    assert _mode(get_secrets_store_file()) == 0o600


def test_lookup_still_works_after_the_change():
    security.create_secret("slack_bot_token", "bot", "xoxb-not-real-1234567890")
    assert security.get_stored_secret("slack_bot_token") == "xoxb-not-real-1234567890"
    assert security.get_stored_secret("missing") is None


def test_the_public_view_still_hides_the_value():
    entry = security.create_secret("k", "d", "hunter2-hunter2")
    assert "value" not in entry
