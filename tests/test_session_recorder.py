"""The bridge from the agent loop to `session_usage`.

Spec: §19.7. The contract is narrow and absolute: recording cost accounting
must never break the call it is accounting for.
"""

import pytest

from gyrfalcon.sessions import recorder
from gyrfalcon.tokenomics import normalize

USAGE = normalize("anthropic", {
    "input_tokens": 100, "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 50, "output_tokens": 20,
})


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

    monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
    monkeypatch.setenv("RUN_MODE", "CLIENT")
    get_gyrfalcon_home.cache_clear()
    recorder.reset_for_tests()
    monkeypatch.setattr("gyrfalcon.sessions.store._store", None, raising=False)
    yield
    recorder.reset_for_tests()
    get_gyrfalcon_home.cache_clear()


class TestRecording:
    def test_records_a_call(self):
        recorder.record_call("sess-1", USAGE, model="claude-sonnet-4-5",
                             provider="anthropic", cost_usd=0.004)
        from gyrfalcon.sessions import get_session_store

        rows = get_session_store().get_usage("sess-1")
        assert len(rows) == 1
        assert rows[0]["cache_read_tokens"] == 900
        assert rows[0]["model"] == "claude-sonnet-4-5"

    def test_creates_the_session_row_if_absent(self):
        """Usage from a conversation whose session lives in the legacy store."""
        recorder.record_call("sess-2", USAGE, model="m", cost_usd=0.001)
        from gyrfalcon.sessions import get_session_store

        assert get_session_store().get_session("sess-2") is not None

    def test_records_the_catalog_version(self):
        recorder.record_call("sess-3", USAGE, model="m", cost_usd=0.001)
        from gyrfalcon.sessions import get_session_store

        assert get_session_store().get_usage("sess-3")[0]["pricing_catalog_version"]


class TestNeverBreaksTheCall:
    def test_a_broken_store_does_not_raise(self, monkeypatch):
        def boom():
            raise RuntimeError("database is on fire")

        monkeypatch.setattr("gyrfalcon.sessions.store.get_session_store", boom)
        recorder.record_call("sess-x", USAGE, model="m", cost_usd=1.0)

    def test_it_disables_itself_after_a_failure(self, monkeypatch):
        """One broken dependency must not become a per-call latency tax."""
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("still on fire")

        monkeypatch.setattr("gyrfalcon.sessions.store.get_session_store", boom)
        for _ in range(5):
            recorder.record_call("sess-x", USAGE, model="m", cost_usd=1.0)
        assert len(calls) == 1

    def test_a_missing_session_id_is_a_no_op(self):
        recorder.record_call(None, USAGE, model="m")
        recorder.record_call("", USAGE, model="m")

    def test_missing_usage_is_a_no_op(self):
        recorder.record_call("sess-y", None, model="m")
