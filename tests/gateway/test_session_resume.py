"""A Slack thread is a conversation, and it must survive the agent cache forgetting
it and the gateway restarting. Against the *real* SessionDB — its ownership
filtering needs a principal, so this also proves the runner binds one around
agent creation, which a fake store cannot."""

from __future__ import annotations

import pytest
import os
from _fakes import DEFAULT_PLATFORM, FakeAdapter, ev, run

from gyrfalcon.gateway.config import GatewayConfig
from gyrfalcon.gateway.run import GatewayRunner
from gyrfalcon.gyrfalcon_state import SessionDB


class DBAgent:
    """Behaves like AIAgent towards the session store: creates a session on the
    first turn and persists each message."""

    def __init__(self, db: SessionDB, session_id=None):
        self.db, self.session_id, self.seen_history = db, session_id, None
        self.interrupted = False

    def reset_interrupt(self):
        pass

    def interrupt(self):
        self.interrupted = True

    def run_conversation(self, user_message, conversation_history=None, **_):
        self.seen_history = conversation_history
        if not self.session_id:
            self.session_id = self.db.create_session(source="slack")
        self.db.append_message(self.session_id, "user", user_message)
        reply = f"echo: {user_message}"
        self.db.append_message(self.session_id, "assistant", reply)
        return {"final_response": reply}


@pytest.fixture()
def db():
    dsn = os.environ.get("GYRFALCON_TEST_PG_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL test DSN required")
    d = SessionDB(dsn=dsn)
    d.set_meta("gateway.session:fake:D1:t1", "")
    yield d
    d.close()


class Gateway:
    """One 'process': a runner + its own agent cache over a (shared) database."""

    def __init__(self, db):
        self.db = db
        self.agents: list[DBAgent] = []
        config = GatewayConfig.from_dict({"platforms": {"fake": DEFAULT_PLATFORM}})
        self.runner = GatewayRunner(config, agent_factory=self._factory, session_db=db, run_scheduler=False)
        self.adapter = FakeAdapter(config.platforms["fake"])

    def _factory(self, src, session_id, settings):
        agent = DBAgent(self.db, session_id)
        self.agents.append(agent)
        return agent

    async def say(self, text, **kw):
        await self.runner.handle_event(self.adapter, ev(text, **kw))


def test_the_thread_to_session_mapping_is_stored_after_the_first_turn(db):
    async def scenario():
        g = Gateway(db)
        await g.say("hello")
        return g
    g = run(scenario())
    assert db.get_meta("gateway.session:fake:D1:t1") == g.agents[0].session_id


def test_a_restarted_gateway_resumes_the_conversation_with_its_history(db):
    async def scenario():
        first = Gateway(db)
        await first.say("my name is Ada")
        second = Gateway(db)          # fresh process: empty agent cache, same database
        await second.say("what is my name?")
        return first, second
    first, second = run(scenario())
    assert second.agents[0].session_id == first.agents[0].session_id, "the same session, not a new one"
    assert second.agents[0].seen_history == [
        {"role": "user", "content": "my name is Ada"},
        {"role": "assistant", "content": "echo: my name is Ada"},
    ]


def test_the_resumed_turn_is_appended_to_the_same_session(db):
    async def scenario():
        await Gateway(db).say("one")
        g = Gateway(db)
        await g.say("two")
        return g
    g = run(scenario())
    contents = [m["content"] for m in db.get_messages(g.agents[0].session_id)]
    assert contents == ["one", "echo: one", "two", "echo: two"]


def test_history_is_replayed_once_then_the_live_agent_carries_it(db):
    async def scenario():
        await Gateway(db).say("one")
        g = Gateway(db)
        await g.say("two")
        await g.say("three")
        return g
    g = run(scenario())
    assert len(g.agents) == 1, "one agent for the thread, not one per message"


def test_a_different_thread_is_a_different_session(db):
    async def scenario():
        g = Gateway(db)
        await g.say("a", thread="t1")
        await g.say("b", thread="t2")
        return g
    g = run(scenario())
    assert g.agents[0].session_id != g.agents[1].session_id


def test_reset_survives_a_restart(db):
    async def scenario():
        g = Gateway(db)
        await g.say("secret plans")
        await g.say("!reset")
        second = Gateway(db)
        await second.say("hello again")
        return g, second
    g, second = run(scenario())
    assert second.agents[0].session_id != g.agents[0].session_id
    assert second.agents[0].seen_history is None, "nothing from before the reset comes back"


def test_a_session_deleted_from_the_database_starts_fresh_instead_of_failing(db):
    async def scenario():
        first = Gateway(db)
        await first.say("hello")
        db.delete_session(first.agents[0].session_id)
        second = Gateway(db)
        await second.say("still there?")
        return first, second
    first, second = run(scenario())
    assert second.agents[0].session_id != first.agents[0].session_id
    assert second.adapter.texts == ["echo: still there?"]


def test_the_stored_mapping_is_per_thread_key_and_survives_other_threads(db):
    async def scenario():
        g = Gateway(db)
        await g.say("a", thread="t1")
        await g.say("b", thread="t2")
        second = Gateway(db)
        await second.say("a again", thread="t1")
        return g, second
    g, second = run(scenario())
    assert second.agents[0].session_id == g.agents[0].session_id
    assert second.agents[0].seen_history[0]["content"] == "a"
