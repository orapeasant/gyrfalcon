"""Sessions, messages and per-call usage, on every configured backend.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.7.

Runs against SQLite always, and PostgreSQL when `GYRFALCON_TEST_PG_DSN` is set
— the two backends have genuinely different full-text machinery (FTS5 shadow
table vs GIN over `to_tsvector`), so testing only one proves little.
"""

import pytest

from gyrfalcon.db.scope import Scope
from gyrfalcon.sessions.store import SessionStore
from gyrfalcon.tokenomics import normalize

ALICE = Scope(tenant_id="acme", user_id="alice")
BOB = Scope(tenant_id="acme", user_id="bob")
OPERATOR = Scope(tenant_id="acme")
OTHER_TENANT = Scope(tenant_id="globex", user_id="alice")


def usage(**kw):
    payload = {"input_tokens": 100, "cache_read_input_tokens": 900,
               "cache_creation_input_tokens": 50, "output_tokens": 20}
    payload.update(kw)
    return normalize("anthropic", payload)


@pytest.fixture()
def store(store_target):
    s = SessionStore(**store_target)
    yield s
    s.close()


class TestSchema:
    def test_migration_applied(self, store):
        assert store.schema_version >= 7


class TestSessions:
    def test_create_and_read_back(self, store):
        sid = store.create_session(source="cli", model="claude-sonnet-4-5",
                                   scope=ALICE)
        row = store.get_session(sid, scope=ALICE)
        assert row["model"] == "claude-sonnet-4-5"
        assert row["tenant_id"] == "acme" and row["user_id"] == "alice"

    def test_totals_start_at_zero(self, store):
        sid = store.create_session(scope=ALICE)
        row = store.get_session(sid, scope=ALICE)
        assert row["uncached_input_tokens"] == 0
        assert row["cache_read_tokens"] == 0
        assert row["cost_usd"] == 0

    def test_title_and_end(self, store):
        sid = store.create_session(scope=ALICE)
        store.update_title(sid, "Cache strategy", scope=ALICE)
        store.end_session(sid, scope=ALICE)
        row = store.get_session(sid, scope=ALICE)
        assert row["title"] == "Cache strategy"
        assert row["ended_at"] is not None

    def test_a_system_scope_cannot_own_a_session(self, store):
        with pytest.raises(ValueError):
            store.create_session(scope=Scope.system(reason="test"))


class TestIsolation:
    """The failure this schema exists to prevent."""

    def test_a_peer_cannot_see_the_session(self, store):
        sid = store.create_session(scope=ALICE)
        assert store.get_session(sid, scope=BOB) is None
        assert store.list_sessions(scope=BOB) == []

    def test_another_tenant_cannot_see_it(self, store):
        sid = store.create_session(scope=ALICE)
        assert store.get_session(sid, scope=OTHER_TENANT) is None

    def test_an_operator_sees_the_whole_tenant(self, store):
        store.create_session(scope=ALICE)
        store.create_session(scope=BOB)
        assert len(store.list_sessions(scope=OPERATOR)) == 2

    def test_an_operator_is_still_confined_to_their_tenant(self, store):
        store.create_session(scope=OTHER_TENANT)
        assert store.list_sessions(scope=OPERATOR) == []

    def test_messages_are_not_visible_across_users(self, store):
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user", "secret plans", scope=ALICE)
        assert store.get_messages(sid, scope=BOB) == []

    def test_usage_is_not_visible_across_users(self, store):
        sid = store.create_session(scope=ALICE)
        store.record_usage(sid, usage(), model="m", scope=ALICE)
        assert store.get_usage(sid, scope=BOB) == []

    def test_a_peer_cannot_delete(self, store):
        sid = store.create_session(scope=ALICE)
        store.delete_session(sid, scope=BOB)
        assert store.get_session(sid, scope=ALICE) is not None


class TestMessages:
    def test_sequence_is_assigned_in_order(self, store):
        sid = store.create_session(scope=ALICE)
        for text in ("one", "two", "three"):
            store.append_message(sid, "user", text, scope=ALICE)
        rows = store.get_messages(sid, scope=ALICE)
        assert [r["seq"] for r in rows] == [0, 1, 2]
        assert [r["content"] for r in rows] == ["one", "two", "three"]

    def test_sequences_are_per_session(self, store):
        a = store.create_session(scope=ALICE)
        b = store.create_session(scope=ALICE)
        store.append_message(a, "user", "x", scope=ALICE)
        store.append_message(b, "user", "y", scope=ALICE)
        assert store.get_messages(b, scope=ALICE)[0]["seq"] == 0


class TestUsage:
    def test_per_call_rows_are_recorded(self, store):
        sid = store.create_session(scope=ALICE)
        store.record_usage(sid, usage(), model="claude-sonnet-4-5",
                           provider="anthropic", cost_usd=0.004,
                           catalog_version="bundled", scope=ALICE)
        store.record_usage(sid, usage(), model="claude-sonnet-4-5",
                           provider="anthropic", cost_usd=0.004, scope=ALICE)
        rows = store.get_usage(sid, scope=ALICE)
        assert [r["seq"] for r in rows] == [0, 1]
        assert rows[0]["cache_read_tokens"] == 900
        assert rows[0]["cache_write_tokens"] == 50

    def test_the_catalog_version_is_pinned_per_row(self, store):
        """A later price change must not silently rewrite history."""
        sid = store.create_session(scope=ALICE)
        store.record_usage(sid, usage(), model="m", cost_usd=1.0,
                           catalog_version="bundled", scope=ALICE)
        assert store.get_usage(sid, scope=ALICE)[0]["pricing_catalog_version"] == "bundled"

    def test_calls_accumulate_onto_the_session(self, store):
        sid = store.create_session(scope=ALICE)
        for _ in range(3):
            store.record_usage(sid, usage(), model="m", cost_usd=0.004,
                               scope=ALICE)
        row = store.get_session(sid, scope=ALICE)
        assert row["uncached_input_tokens"] == 300
        assert row["cache_read_tokens"] == 2700
        assert row["cache_write_tokens"] == 150
        assert row["output_tokens"] == 60
        assert row["cost_usd"] == pytest.approx(0.012)

    def test_the_three_input_classes_do_not_overlap(self, store):
        """The §19.6 invariant, carried all the way into storage."""
        sid = store.create_session(scope=ALICE)
        store.record_usage(sid, usage(), model="m", scope=ALICE)
        row = store.get_usage(sid, scope=ALICE)[0]
        total = (row["uncached_input_tokens"] + row["cache_read_tokens"]
                 + row["cache_write_tokens"])
        assert total == 1050

    def test_usage_by_model_aggregates(self, store):
        sid = store.create_session(scope=ALICE)
        store.record_usage(sid, usage(), model="a", cost_usd=1.0, scope=ALICE)
        store.record_usage(sid, usage(), model="a", cost_usd=2.0, scope=ALICE)
        store.record_usage(sid, usage(), model="b", cost_usd=0.5, scope=ALICE)
        by_model = {r["model"]: r for r in store.usage_by_model(scope=ALICE)}
        assert by_model["a"]["calls"] == 2
        assert by_model["a"]["cost_usd"] == pytest.approx(3.0)
        assert by_model["b"]["calls"] == 1

    def test_usage_by_model_is_scoped(self, store):
        sid = store.create_session(scope=ALICE)
        store.record_usage(sid, usage(), model="a", cost_usd=1.0, scope=ALICE)
        assert store.usage_by_model(scope=OTHER_TENANT) == []


class TestSearch:
    """FTS5 and tsvector are different machinery; both must behave the same."""

    def test_finds_a_message_by_word(self, store):
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user",
                             "How do I cache prompts effectively?", scope=ALICE)
        hits = store.search_messages("cache", scope=ALICE)
        assert len(hits) == 1
        assert "cache" in hits[0]["content"]

    def test_results_carry_the_session_title(self, store):
        sid = store.create_session(title="Caching", scope=ALICE)
        store.append_message(sid, "user", "cache me", scope=ALICE)
        assert store.search_messages("cache", scope=ALICE)[0]["session_title"] == "Caching"

    def test_search_is_scoped_to_the_caller(self, store):
        """Message text is the most sensitive data here."""
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user", "confidential cache design", scope=ALICE)
        assert store.search_messages("confidential", scope=BOB) == []
        assert store.search_messages("confidential", scope=OTHER_TENANT) == []

    def test_no_match_returns_empty(self, store):
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user", "hello", scope=ALICE)
        assert store.search_messages("zzzznotpresent", scope=ALICE) == []

    def test_a_malformed_query_does_not_raise(self, store):
        """An ordinary search box must not 500 on stray punctuation."""
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user", "hello", scope=ALICE)
        assert store.search_messages('NEAR("', scope=ALICE) == []
        assert store.search_messages("*", scope=ALICE) == []

    def test_deleted_messages_stop_being_searchable(self, store):
        """The FTS delete trigger — without it, text outlives its delete."""
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user", "ephemeral secret", scope=ALICE)
        assert store.search_messages("ephemeral", scope=ALICE)
        store.delete_session(sid, scope=ALICE)
        assert store.search_messages("ephemeral", scope=ALICE) == []

    def test_sessions_are_searchable_by_title(self, store):
        store.create_session(title="Quarterly costs", scope=ALICE)
        assert len(store.search_sessions("Quarterly", scope=ALICE)) == 1
        assert store.search_sessions("Quarterly", scope=BOB) == []


class TestDeletion:
    def test_delete_removes_children(self, store):
        sid = store.create_session(scope=ALICE)
        store.append_message(sid, "user", "hi", scope=ALICE)
        store.record_usage(sid, usage(), model="m", scope=ALICE)
        store.delete_session(sid, scope=ALICE)
        assert store.get_session(sid, scope=ALICE) is None
        assert store.get_messages(sid, scope=ALICE) == []
        assert store.get_usage(sid, scope=ALICE) == []

    def test_delete_leaves_other_sessions_alone(self, store):
        keep = store.create_session(scope=ALICE)
        drop = store.create_session(scope=ALICE)
        store.append_message(keep, "user", "keep me", scope=ALICE)
        store.delete_session(drop, scope=ALICE)
        assert store.get_session(keep, scope=ALICE) is not None
        assert len(store.get_messages(keep, scope=ALICE)) == 1


class TestEnsureSession:
    def test_creates_when_absent(self, store):
        assert store.ensure_session("abc123", model="m", scope=ALICE) is True
        assert store.get_session("abc123", scope=ALICE) is not None

    def test_is_idempotent(self, store):
        store.ensure_session("abc123", scope=ALICE)
        assert store.ensure_session("abc123", scope=ALICE) is False
        assert len(store.list_sessions(scope=ALICE)) == 1
