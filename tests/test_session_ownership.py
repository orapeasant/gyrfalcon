"""Session ownership — §17.11 step 4.

`sessions.user_id` existed, was written as None on every insert, and was never
read by any query (§17.1). This is the suite that makes it load-bearing: the
column now carries the owning principal, and every read filters on it.
"""

from __future__ import annotations

import pytest
import os

from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.identity import Principal, use_principal

TEST_PG_DSN = os.environ.get("GYRFALCON_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not TEST_PG_DSN, reason="Disposable PostgreSQL test DSN required")


@pytest.fixture()
def db():
    d = SessionDB(dsn=TEST_PG_DSN)
    with d._db.connect() as conn:
        conn.execute("TRUNCATE ai_session_messages, ai_session_usage, ai_sessions RESTART IDENTITY CASCADE")
    yield d
    d.close() if hasattr(d, "close") else None


def as_user(user, roles=()):
    return use_principal(Principal(user_id=user, roles=roles))


class TestOwnership:
    def test_a_session_records_its_creator(self, db):
        with as_user("alice"):
            sid = db.create_session(source="chat")
            assert db.get_session(sid)["user_id"] == "alice"

    def test_single_user_installs_own_their_sessions_as_local(self, db):
        sid = db.create_session(source="chat")
        assert db.get_session(sid)["user_id"] == "local"

    def test_another_user_cannot_read_a_session(self, db):
        with as_user("alice"):
            sid = db.create_session(source="chat")
        with as_user("bob"):
            assert db.get_session(sid) is None

    def test_listing_is_scoped(self, db):
        with as_user("alice"):
            db.create_session(source="chat")
            db.create_session(source="chat")
        with as_user("bob"):
            db.create_session(source="chat")
            assert len(db.list_sessions()) == 1
        with as_user("alice"):
            assert len(db.list_sessions()) == 2

    def test_source_filter_composes_with_the_owner_filter(self, db):
        with as_user("alice"):
            db.create_session(source="chat")
            db.create_session(source="cli")
        with as_user("bob"):
            db.create_session(source="chat")
        with as_user("alice"):
            assert len(db.list_sessions(source="chat")) == 1

    def test_an_operator_sees_every_session(self, db):
        with as_user("alice"):
            db.create_session(source="chat")
        with as_user("bob"):
            db.create_session(source="chat")
        with as_user("ops", roles=["operator"]):
            assert len(db.list_sessions()) == 2

    def test_title_search_is_scoped(self, db):
        with as_user("alice"):
            db.create_session(source="chat", title="alice secret plan")
        with as_user("bob"):
            assert db.search_sessions("secret") == []
        with as_user("alice"):
            assert len(db.search_sessions("secret")) == 1

    def test_message_search_is_scoped(self, db):
        with as_user("alice"):
            sid = db.create_session(source="chat")
            db.append_message(sid, "user", "the passphrase is hunter2")
        with as_user("bob"):
            assert db.search_messages("hunter2") == []
        with as_user("alice"):
            assert len(db.search_messages("hunter2")) == 1

    def test_deleting_someone_elses_session_is_refused(self, db):
        with as_user("alice"):
            sid = db.create_session(source="chat")
        with as_user("bob"):
            db.delete_session(sid)
        with as_user("alice"):
            assert db.get_session(sid) is not None, "delete crossed an owner boundary"

    def test_deleting_your_own_session_works(self, db):
        with as_user("alice"):
            sid = db.create_session(source="chat")
            db.delete_session(sid)
            assert db.get_session(sid) is None

    def test_get_messages_follows_session_ownership(self, db):
        with as_user("alice"):
            sid = db.create_session(source="chat")
            db.append_message(sid, "user", "alice secret plan")
        with as_user("bob"):
            assert db.get_messages(sid) == [], (
                "bob must not be able to read alice's messages by session id"
            )

    def test_get_messages_as_conversation_follows_session_ownership(self, db):
        """Same root cause as get_messages: this just formats what it returns."""
        with as_user("alice"):
            sid = db.create_session(source="chat")
            db.append_message(sid, "user", "alice secret plan")
        with as_user("bob"):
            assert db.get_messages_as_conversation(sid) == []
