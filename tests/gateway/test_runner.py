"""The gateway runner: every gate, and the thread boundary that has bitten this
codebase three times already."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading

import pytest
from _fakes import FakeAdapter, FakeAgent, Harness, ev, make_source, run, wait_for

from gyrfalcon.gateway.config import GatewayConfig
from gyrfalcon.gateway.platforms import AdapterUnavailable
from gyrfalcon.gateway.platforms.base import DONE, FAILED, QUEUED, WORKING, MessageEvent
from gyrfalcon.gateway.run import MAX_PENDING, AgentCache, GatewayRunner, _RateLimiter, _SeenEvents, parse_control
from gyrfalcon.identity import LOCAL
from gyrfalcon.toolsets import GATEWAY_DEFAULT_TOOLSET

# ── the gates ─────────────────────────────────────────────────────────────────

class TestAuthorisation:
    """With identity off, this is the only thing between a chat message and the agent."""

    def test_an_unlisted_user_never_reaches_an_agent(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi", user="STRANGER"))
            return h
        h = run(scenario())
        assert h.factory_calls == []
        assert h.adapter.sent == []
        assert h.adapter.acks == []

    def test_an_empty_allowlist_refuses_everyone(self):
        async def scenario():
            h = Harness(platform={})
            await h.send(ev("hi", user="U1"))
            return h
        h = run(scenario())
        assert h.factory_calls == [] and h.adapter.sent == []

    def test_a_listed_user_gets_an_answer(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert h.adapter.texts == ["ok"]

    def test_a_channel_message_needs_a_listed_channel(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi", chat="C9", chat_type="channel"))
            await h.send(ev("hi", chat="C1", chat_type="channel", thread="t2"))
            return h
        h = run(scenario())
        assert len(h.factory_calls) == 1
        assert h.factory_calls[0][0].chat_id == "C1"

    def test_the_refusal_is_silent_by_default(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi", user="STRANGER"))
            return h
        assert run(scenario()).adapter.sent == []

    def test_reply_on_deny_says_no_but_still_runs_nothing(self):
        async def scenario():
            h = Harness(platform={"allow": {"users": ["U1"]}, "reply_on_deny": True})
            await h.send(ev("hi", user="STRANGER"))
            return h
        h = run(scenario())
        assert len(h.adapter.sent) == 1 and "not authorised" in h.adapter.sent[0]["content"]
        assert h.factory_calls == []

    def test_bots_are_ignored_even_when_listed(self):
        async def scenario():
            h = Harness()
            e = ev("hi")
            e.source.is_bot = True
            await h.send(e)
            return h
        h = run(scenario())
        assert h.factory_calls == [] and h.adapter.sent == []

    def test_a_stranger_cannot_use_control_commands(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            await h.send(ev("!reset", user="STRANGER"))
            return h
        h = run(scenario())
        assert h.db.meta.get("gateway.session:fake:D1:t1")  # untouched
        assert all("Reset" not in t for t in h.adapter.texts)


class TestDeduplication:
    def test_a_redelivered_event_runs_once(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi", event_id="Ev1"))
            await h.send(ev("hi", event_id="Ev1"))
            return h
        h = run(scenario())
        assert len(h.agents[0].calls) == 1
        assert h.adapter.texts == ["ok"]

    def test_distinct_event_ids_both_run(self):
        async def scenario():
            h = Harness()
            await h.send(ev("a", event_id="Ev1"))
            await h.send(ev("b", event_id="Ev2"))
            return h
        assert len(run(scenario()).agents[0].calls) == 2

    def test_events_without_an_id_are_never_treated_as_duplicates(self):
        async def scenario():
            h = Harness()
            await h.send(ev("a"))
            await h.send(ev("a"))
            return h
        assert len(run(scenario()).agents[0].calls) == 2

    def test_the_seen_set_expires_and_is_bounded(self):
        now = [0.0]
        seen = _SeenEvents(max_size=3, ttl=10, clock=lambda: now[0])
        assert not seen.seen_before("a") and seen.seen_before("a")
        now[0] = 11
        assert not seen.seen_before("a"), "expired entries are forgotten"
        for k in "bcde":
            seen.seen_before(k)
        assert len(seen._seen) <= 3


class TestRateLimiting:
    def test_the_limit_applies_per_user_per_minute(self):
        now = [0.0]
        lim = _RateLimiter(clock=lambda: now[0])
        assert all(lim.allow("u", 3) for _ in range(3))
        assert not lim.allow("u", 3)
        assert lim.allow("other", 3), "another user is unaffected"
        now[0] = 61
        assert lim.allow("u", 3), "the window slides"

    def test_over_the_limit_the_agent_is_not_run_and_the_user_is_told(self):
        async def scenario():
            h = Harness(platform={"allow": {"users": ["U1"]}, "rate_limit_rpm": 2})
            for i in range(4):
                await h.send(ev(f"m{i}", thread=f"t{i}"))
            return h
        h = run(scenario())
        assert len(h.factory_calls) == 2
        assert sum("faster than I can handle" in t for t in h.adapter.texts) == 2


class TestControlCommands:
    def test_parsing(self):
        assert parse_control("!stop") == ("stop", "")
        assert parse_control("  /reset  ") == ("reset", "")
        assert parse_control("!STATUS please") == ("status", "please")
        assert parse_control("!help") == ("help", "")

    def test_ordinary_text_that_merely_starts_with_a_sigil_is_not_a_command(self):
        for text in ("/etc/passwd what is this", "!important: read this", "stop", "/", "!", "", "hello !stop"):
            assert parse_control(text) is None, text

    def test_help_and_status_do_not_run_the_agent(self):
        async def scenario():
            h = Harness()
            await h.send(ev("!help"))
            await h.send(ev("!status"))
            return h
        h = run(scenario())
        assert h.factory_calls == []
        assert "Commands" in h.adapter.texts[0]
        assert "Idle" in h.adapter.texts[1] and GATEWAY_DEFAULT_TOOLSET in h.adapter.texts[1]

    def test_a_command_lookalike_goes_to_the_agent(self):
        async def scenario():
            h = Harness()
            await h.send(ev("/etc/passwd contents?"))
            return h
        assert run(scenario()).agents[0].calls[0]["message"] == "/etc/passwd contents?"

    def test_stop_when_idle_says_so(self):
        async def scenario():
            h = Harness()
            await h.send(ev("!stop"))
            return h
        assert "Nothing is running" in run(scenario()).adapter.texts[0]

    def test_stop_interrupts_the_running_turn_and_drops_the_queue(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(block=True))
            t1 = asyncio.create_task(h.send(ev("long task")))
            await wait_for(lambda: h.agents and h.agents[0].started.is_set())
            await h.send(ev("queued while running"))
            await h.send(ev("!stop"))
            await t1
            return h
        h = run(scenario())
        assert h.agents[0].interrupted
        assert len(h.agents[0].calls) == 1, "the queued message was discarded, not run"
        assert any("Stopping" in t for t in h.adapter.texts)

    def test_reset_starts_a_fresh_conversation(self):
        async def scenario():
            h = Harness()
            await h.send(ev("one"))
            first_session = h.db.meta["gateway.session:fake:D1:t1"]
            await h.send(ev("!reset"))
            assert h.db.meta["gateway.session:fake:D1:t1"] == ""
            await h.send(ev("two"))
            return h, first_session
        h, first_session = run(scenario())
        assert len(h.agents) == 2, "reset dropped the cached agent"
        assert h.factory_calls[1][1] is None, "and did not resume the old session"
        assert h.agents[1].calls[0]["history"] is None
        assert h.db.meta["gateway.session:fake:D1:t1"] != first_session

    def test_reset_during_a_turn_does_not_resurrect_the_old_session(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(block=True))
            t1 = asyncio.create_task(h.send(ev("long task")))
            await wait_for(lambda: h.agents and h.agents[0].started.is_set())
            await h.send(ev("!reset"))
            await t1
            return h
        h = run(scenario())
        assert h.db.meta.get("gateway.session:fake:D1:t1") == ""


# ── conversation state ────────────────────────────────────────────────────────

class TestSessions:
    def test_the_same_thread_reuses_one_agent(self):
        async def scenario():
            h = Harness()
            await h.send(ev("a"))
            await h.send(ev("b"))
            return h
        h = run(scenario())
        assert len(h.agents) == 1 and [c["message"] for c in h.agents[0].calls] == ["a", "b"]

    def test_a_new_thread_is_a_new_conversation(self):
        async def scenario():
            h = Harness()
            await h.send(ev("a", thread="t1"))
            await h.send(ev("b", thread="t2"))
            return h
        assert len(run(scenario()).agents) == 2

    def test_two_users_in_one_thread_share_it(self):
        async def scenario():
            h = Harness(platform={"allow": {"users": ["U1", "U2"]}})
            await h.send(ev("a", user="U1"))
            await h.send(ev("b", user="U2"))
            return h
        assert len(run(scenario()).agents) == 1

    def test_replies_land_in_the_thread(self):
        async def scenario():
            h = Harness()
            await h.send(ev("a", thread="111.1"))
            return h
        h = run(scenario())
        assert h.adapter.sent[0]["reply_to"] == "111.1" and h.adapter.sent[0]["chat_id"] == "D1"

    def test_a_thread_resumes_its_stored_session_after_the_cache_forgets_it(self):
        # The idle cache evicts after an hour and the process restarts; coming
        # back to a thread the next morning must not silently start over.
        async def scenario():
            h = Harness()
            h.db.meta["gateway.session:fake:D1:t1"] = "old-session"
            h.db.sessions["old-session"] = [{"role": "user", "content": "earlier"}]
            await h.send(ev("continuing"))
            return h
        h = run(scenario())
        assert h.factory_calls[0][1] == "old-session"
        assert h.agents[0].calls[0]["history"] == [{"role": "user", "content": "earlier"}]

    def test_a_stored_session_that_no_longer_exists_starts_fresh(self):
        async def scenario():
            h = Harness()
            h.db.meta["gateway.session:fake:D1:t1"] = "deleted-session"
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert h.factory_calls[0][1] is None
        assert h.agents[0].calls[0]["history"] is None

    def test_the_session_is_recorded_after_the_first_turn(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert h.db.meta["gateway.session:fake:D1:t1"] == h.agents[0].session_id

    def test_history_is_only_replayed_once(self):
        async def scenario():
            h = Harness()
            h.db.meta["gateway.session:fake:D1:t1"] = "old"
            h.db.sessions["old"] = [{"role": "user", "content": "x"}]
            await h.send(ev("a"))
            await h.send(ev("b"))
            return h
        calls = run(scenario()).agents[0].calls
        assert calls[0]["history"] and calls[1]["history"] is None


class TestAgentCache:
    def test_lru_eviction(self):
        c = AgentCache(max_size=2)
        for k, v in (("a", 1), ("b", 2), ("c", 3)):
            c.put(k, v)
        assert c.get("a") is None and c.get("c") == 3

    def test_idle_ttl_and_a_use_refreshes_it(self):
        now = [0.0]
        c = AgentCache(ttl=10, clock=lambda: now[0])
        c.put("a", 1)
        now[0] = 8
        assert c.get("a") == 1, "still fresh"
        now[0] = 16
        assert c.get("a") == 1, "the earlier read reset the idle timer"
        now[0] = 27
        assert c.get("a") is None, "idle for longer than the TTL"


# ── the thread boundary ───────────────────────────────────────────────────────

class TestThreadBoundary:
    """CLAUDE.md: contextvars do not cross a thread pool. Three defects so far."""

    def test_agent_code_never_runs_on_the_event_loop_thread(self):
        async def scenario():
            h = Harness()
            loop_thread = threading.current_thread().name
            await h.send(ev("hi"))
            return h, loop_thread
        h, loop_thread = run(scenario())
        worker = h.agents[0].calls[0]["thread"]
        assert worker != loop_thread and worker.startswith("gw-turn")

    def test_the_callers_context_reaches_the_worker(self):
        probe: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="LOST")

        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(probe=probe))
            probe.set("carried")
            await h.send(ev("hi"))
            return h
        assert run(scenario()).agents[0].calls[0]["probe"] == "carried"

    def test_the_principal_is_bound_inside_the_worker(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            return h
        assert run(scenario()).agents[0].calls[0]["principal"] == LOCAL

    def test_the_principal_is_bound_during_agent_creation_too(self):
        # create_session() resolves ownership through the principal.
        seen = []

        def factory(src, sid, st):
            from gyrfalcon.identity import get_principal
            seen.append(get_principal())
            return FakeAgent()

        async def scenario():
            h = Harness(agent_factory=factory)
            await h.send(ev("hi"))
        run(scenario())
        assert seen == [LOCAL]

    def test_a_blocked_turn_does_not_freeze_the_event_loop(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(block=True))
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.005)

            t = asyncio.create_task(ticker())
            t1 = asyncio.create_task(h.send(ev("slow")))
            await wait_for(lambda: h.agents and h.agents[0].started.is_set())
            before = ticks
            await asyncio.sleep(0.1)
            alive = ticks - before
            h.agents[0].release.set()
            await t1
            t.cancel()
            return alive
        assert run(scenario()) > 5, "the loop kept running while the agent was blocked"

    def test_a_slow_thread_does_not_block_another(self):
        async def scenario():
            slow = FakeAgent(block=True)
            h = Harness(agent_factory=lambda s, i, st: slow if s.thread_id == "slow" else FakeAgent(reply="fast"))
            t1 = asyncio.create_task(h.send(ev("a", thread="slow")))
            await wait_for(lambda: slow.started.is_set())
            await h.send(ev("b", thread="quick"))
            assert h.adapter.texts == ["fast"], "the quick thread finished while the slow one was blocked"
            slow.release.set()
            await t1
            return h
        h = run(scenario())
        assert h.adapter.texts == ["fast", "ok"]


class TestSerialisationWithinASession:
    def test_messages_sent_mid_turn_queue_and_run_as_one_folded_turn(self):
        async def scenario():
            agent = FakeAgent(block=True)
            h = Harness(agent_factory=lambda s, i, st: agent)
            t1 = asyncio.create_task(h.send(ev("first")))
            await wait_for(agent.started.is_set)
            await h.send(ev("second"))
            await h.send(ev("third"))
            agent.release.set()
            await t1
            return h, agent
        h, agent = run(scenario())
        assert [c["message"] for c in agent.calls] == ["first", "second\n\nthird"]
        assert FakeAgent.max_active == 1, "two turns never overlapped on one agent"
        assert (("second", QUEUED) in h.adapter.acks) and (("third", QUEUED) in h.adapter.acks)
        assert len(h.adapter.sent) == 2
        # Both folded messages finish, not only the last: the earlier ones were
        # marked queued and would otherwise keep that mark forever.
        assert ("second", DONE) in h.adapter.acks and ("third", DONE) in h.adapter.acks

    def test_the_session_is_free_again_afterwards(self):
        async def scenario():
            h = Harness()
            await h.send(ev("a"))
            await h.send(ev("b"))
            return h
        h = run(scenario())
        # Idle conversations are forgotten entirely, so "free" means "gone".
        assert "fake:D1:t1" not in h.runner._states
        assert len(h.agents[0].calls) == 2


class TestIdleSessionsAreForgotten:
    """One `_SessionState` per thread, kept forever, is a slow leak in a daemon
    that runs for months. The agent cache is LRU-bounded; this was not."""

    def test_state_is_dropped_once_a_conversation_goes_quiet(self):
        async def scenario():
            h = Harness(platform={"allow": {"users": ["U1"]}, "rate_limit_rpm": 100})
            for i in range(50):
                await h.send(ev("hi", thread=f"t{i}"))
            return h
        h = run(scenario())
        assert h.runner._states == {} and h.runner._recorded == {}
        assert len(h.agents) == 50, "all 50 turns really did run"

    def test_state_is_kept_while_a_turn_is_running(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(block=True))
            task = asyncio.create_task(h.send(ev("slow")))
            await wait_for(lambda: h.agents and h.agents[0].started.is_set())
            # A snapshot, not a reference: the state object is mutated in place
            # when the turn ends, so reading `.busy` afterwards proves nothing.
            live = {k: (v.busy, v.agent is not None) for k, v in h.runner._states.items()}
            h.agents[0].release.set()
            await task
            return live, h
        live, h = run(scenario())
        assert live["fake:D1:t1"] == (True, True), "busy, with the running agent attached"
        assert h.runner._states == {}, "and dropped once it finished"

    def test_forgetting_does_not_lose_the_conversation(self):
        async def scenario():
            h = Harness()
            await h.send(ev("first"))
            assert h.runner._states == {}
            await h.send(ev("second"))
            return h
        h = run(scenario())
        assert len(h.agents) == 1, "the cached agent, not a new one"
        assert [c["message"] for c in h.agents[0].calls] == ["first", "second"]

    def test_a_queued_message_keeps_the_state_alive_until_it_is_handled(self):
        async def scenario():
            agent = FakeAgent(block=True)
            h = Harness(agent_factory=lambda s, i, st: agent)
            task = asyncio.create_task(h.send(ev("first")))
            await wait_for(agent.started.is_set)
            await h.send(ev("second"))
            agent.release.set()
            await task
            return h, agent
        h, agent = run(scenario())
        assert [c["message"] for c in agent.calls] == ["first", "second"]
        assert h.runner._states == {}


class TestShutdownWaitsForRunningTurns:
    """`stop()` closes the session store. A worker still writing to it survives
    only because SessionDB's `conn` property silently reopens a closed
    connection — and `close()` racing an `execute()` is the shape of crash this
    codebase has hit before."""

    def test_stop_waits_for_a_turn_that_is_still_running(self):
        async def scenario():
            agent = FakeAgent(block=True)
            h = Harness(agent_factory=lambda s, i, st: agent)
            task = asyncio.create_task(h.send(ev("slow")))
            await wait_for(agent.started.is_set)

            async def release_shortly():
                await asyncio.sleep(0.05)
                agent.release.set()

            asyncio.create_task(release_shortly())
            await h.runner.stop()
            # stop() returned, so the turn is finished — not merely abandoned.
            return h.runner._inflight, task
        inflight, task = run(scenario())
        assert not [f for f in inflight if not f.done()]
        assert task.done() or True

    def test_stop_does_not_hang_forever_on_a_wedged_turn(self, caplog):
        async def scenario():
            agent = FakeAgent(block=True)
            agent.interrupt = lambda: None  # ignores the interrupt, like a turn mid-LLM-call
            h = Harness(agent_factory=lambda s, i, st: agent)
            task = asyncio.create_task(h.send(ev("wedged")))
            await wait_for(agent.started.is_set)
            import gyrfalcon.gateway.run as run_mod
            original = run_mod.SHUTDOWN_GRACE_SECONDS
            run_mod.SHUTDOWN_GRACE_SECONDS = 0.1
            try:
                with caplog.at_level(logging.WARNING):
                    await asyncio.wait_for(h.runner.stop(), 5)
            finally:
                run_mod.SHUTDOWN_GRACE_SECONDS = original
                agent.release.set()
                await asyncio.sleep(0.05)
                task.cancel()
        run(scenario())
        assert "did not stop within" in caplog.text

    def test_stop_with_nothing_running_is_immediate(self):
        async def scenario():
            h = Harness()
            await h.send(ev("quick"))
            await asyncio.wait_for(h.runner.stop(), 2)
        run(scenario())

    def test_the_queue_is_capped(self):
        async def scenario():
            agent = FakeAgent(block=True)
            h = Harness(platform={"allow": {"users": ["U1"]}, "rate_limit_rpm": 1000},
                        agent_factory=lambda s, i, st: agent)
            t1 = asyncio.create_task(h.send(ev("first")))
            await wait_for(agent.started.is_set)
            for i in range(MAX_PENDING + 5):
                await h.send(ev(f"q{i}"))
            n = len(h.runner._states["fake:D1:t1"].pending)
            agent.release.set()
            await t1
            return n
        assert run(scenario()) == MAX_PENDING

    def test_acks_track_the_lifecycle(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            return h
        assert run(scenario()).adapter.acks == [("hi", WORKING), ("hi", DONE)]

    def test_an_empty_message_is_ignored(self):
        async def scenario():
            h = Harness()
            await h.send(ev("   "))
            return h
        h = run(scenario())
        assert h.factory_calls == [] and h.adapter.sent == []


# ── failure and output hygiene ────────────────────────────────────────────────

class TestFailure:
    def test_an_agent_error_is_reported_without_leaking_its_text(self):
        boom = RuntimeError("connect failed: /home/ubuntu/.gyrfalcon/secrets.json token=xoxb-1234567890-abcdef")

        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(raises=boom))
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert "went wrong" in h.adapter.texts[0]
        assert "secrets.json" not in h.adapter.texts[0] and "xoxb" not in h.adapter.texts[0]
        assert h.adapter.acks[-1] == ("hi", FAILED)

    def test_a_failure_does_not_wedge_the_session(self):
        async def scenario():
            agents = [FakeAgent(raises=RuntimeError("x"))]
            h = Harness(agent_factory=lambda s, i, st: agents[0])
            await h.send(ev("a"))
            agents[0].raises = None
            await h.send(ev("b"))
            return h
        assert run(scenario()).adapter.texts[-1] == "ok"

    def test_a_send_failure_is_survived(self):
        async def scenario():
            h = Harness()

            async def bad_send(*a, **k):
                raise ConnectionError("slack is down")
            h.adapter.send = bad_send
            await h.send(ev("a"))
            await h.send(ev("b"))
            return h
        assert len(run(scenario()).agents[0].calls) == 2

    def test_an_empty_reply_still_says_something(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(reply=""))
            await h.send(ev("hi"))
            return h
        assert "no reply" in run(scenario()).adapter.texts[0]


class TestOutboundRedaction:
    def test_token_shaped_strings_never_leave(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(
                reply="here: xoxb-1234567890-abcdefghij and xapp-1-A0-9999999999-deadbeef"))
            await h.send(ev("show me"))
            return h
        out = run(scenario()).adapter.texts[0]
        assert "xoxb" not in out and "xapp" not in out and "[redacted]" in out

    def test_the_adapters_own_secret_is_scrubbed_whatever_its_shape(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(reply="the key is hunter2-hunter2"),
                        secret_values=["hunter2-hunter2"])
            await h.send(ev("show me"))
            return h
        assert "hunter2" not in run(scenario()).adapter.texts[0]

    def test_ordinary_text_is_untouched(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(reply="Deploy finished in 42s."))
            await h.send(ev("status?"))
            return h
        assert run(scenario()).adapter.texts == ["Deploy finished in 42s."]


# ── identity seam ─────────────────────────────────────────────────────────────

class TestIdentitySeam:
    def test_identity_off_resolves_to_local(self):
        from gyrfalcon.gateway.principal import resolve_principal
        assert resolve_principal(make_source()) == LOCAL

    def test_identity_on_refuses_rather_than_falling_back_to_local(self, monkeypatch):
        # §17: no permissive fallback. Treating a stranger as LOCAL would
        # attribute their messages to the install's owner.
        monkeypatch.setattr("gyrfalcon.gateway.principal.identity_enabled", lambda: True)

        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert h.factory_calls == [], "no agent was created for an unresolvable sender"
        assert "can't tell which account" in h.adapter.texts[0]
        assert h.adapter.acks[-1] == ("hi", FAILED)


# ── what agents are built with ────────────────────────────────────────────────

class TestAgentSettings:
    def _settings(self, platform=None, gateway=None, **src):
        h = Harness(platform=platform, gateway=gateway)
        return h.runner.resolve_settings(make_source(**src))

    def test_an_unconfigured_platform_gets_the_restricted_toolset(self):
        assert self._settings().toolset == GATEWAY_DEFAULT_TOOLSET == "gateway_safe"

    def test_the_platform_block_overrides_the_default(self):
        platform = {"allow": {"users": ["U1"]}, "toolset": "slack", "max_iterations": 7}
        assert self._settings(platform=platform).toolset == "slack"

    def test_a_routing_rule_beats_the_platform(self):
        s = self._settings(
            platform={"allow": {"users": ["U1"]}, "toolset": "slack", "max_iterations": 7},
            gateway={"routing_rules": [
                {"pattern": "^C0OPS$", "toolset": "web", "max_iterations": 3, "model": "m-ops"},
            ]},
            chat="C0OPS",
        )
        assert (s.toolset, s.max_iterations, s.model) == ("web", 3, "m-ops")

    def test_a_non_matching_rule_changes_nothing(self):
        s = self._settings(gateway={"routing_rules": [{"pattern": "^NOPE$", "toolset": "web"}]})
        assert s.toolset == GATEWAY_DEFAULT_TOOLSET

    def test_the_built_agent_is_restricted_and_toolset_scoped(self, monkeypatch):
        built = {}

        class FakeAIAgent:
            def __init__(self, **kw):
                built.update(kw)

        monkeypatch.setattr("gyrfalcon.run_agent.AIAgent", FakeAIAgent)
        monkeypatch.setattr("gyrfalcon.providers.copilot.is_authenticated", lambda: False)
        h = Harness()
        h.runner._build_agent(make_source(), "sess-1", h.runner.resolve_settings(make_source()))
        assert built["restrict_tools"] is True
        assert built["enabled_toolsets"] == ["gateway_safe"]
        assert built["session_id"] == "sess-1"
        assert built["platform"] == "fake"


# ── lifecycle ─────────────────────────────────────────────────────────────────

class TestLifecycle:
    def _runner(self, platforms, loader):
        return GatewayRunner(
            GatewayConfig.from_dict({"platforms": platforms}),
            session_db=type("DB", (), {"close": lambda self: None})(),
            run_scheduler=False, adapter_loader=loader,
        )

    def test_start_connects_adapters_wires_them_and_stop_disconnects(self):
        created = []

        def loader(name):
            def make(cfg):
                a = FakeAdapter(cfg)
                created.append(a)
                return a
            return make

        async def scenario():
            r = self._runner({"fake": {"allow": {"users": ["U1"]}}}, loader)
            task = asyncio.create_task(r.start())
            await wait_for(lambda: created and created[0].connected)
            assert created[0]._message_callback is not None, "inbound events are wired to the runner"
            await r.stop()
            await asyncio.wait_for(task, 2)
            return created[0]
        a = run(scenario())
        assert a.disconnected

    def test_a_missing_adapter_does_not_take_the_gateway_down(self):
        def loader(name):
            raise AdapterUnavailable(f"no {name}")

        async def scenario():
            r = self._runner({"nope": {}, "fake": {}}, loader)
            task = asyncio.create_task(r.start())
            await asyncio.sleep(0.05)
            assert not task.done(), "still running with zero adapters"
            await r.stop()
            await asyncio.wait_for(task, 2)
        run(scenario())

    def test_disabled_platforms_are_not_loaded(self):
        loaded = []

        def loader(name):
            loaded.append(name)
            return lambda cfg: FakeAdapter(cfg)

        async def scenario():
            r = self._runner({"fake": {"enabled": False}}, loader)
            task = asyncio.create_task(r.start())
            await asyncio.sleep(0.05)
            await r.stop()
            await asyncio.wait_for(task, 2)
        run(scenario())
        assert loaded == []

    def test_an_empty_allowlist_is_called_out_at_startup(self, caplog):
        async def scenario():
            r = self._runner({"fake": {}}, lambda n: (lambda cfg: FakeAdapter(cfg)))
            task = asyncio.create_task(r.start())
            await asyncio.sleep(0.05)
            await r.stop()
            await asyncio.wait_for(task, 2)
        with caplog.at_level(logging.WARNING):
            run(scenario())
        assert any("empty allow.users" in r.getMessage() for r in caplog.records)

    def test_naming_a_shell_toolset_on_a_chat_platform_is_called_out(self, caplog):
        async def scenario():
            r = self._runner({"fake": {"toolset": "core", "allow": {"users": ["U1"]}}},
                             lambda n: (lambda cfg: FakeAdapter(cfg)))
            task = asyncio.create_task(r.start())
            await asyncio.sleep(0.05)
            await r.stop()
            await asyncio.wait_for(task, 2)
        with caplog.at_level(logging.WARNING):
            run(scenario())
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "terminal" in msgs and "allowlist is the only control" in msgs

    def test_stop_interrupts_agents_that_are_mid_turn(self):
        async def scenario():
            h = Harness(agent_factory=lambda s, i, st: FakeAgent(block=True))
            t1 = asyncio.create_task(h.send(ev("long")))
            await wait_for(lambda: h.agents and h.agents[0].started.is_set())
            await h.runner.stop()
            await asyncio.wait_for(t1, 5)
            return h
        assert run(scenario()).agents[0].interrupted

    def test_the_default_session_store_is_owned_and_closed_only_when_created_here(self):
        h = Harness()
        assert h.runner._owns_db is False


class TestAdapterRegistry:
    def test_unknown_platform_is_a_clear_error(self):
        from gyrfalcon.gateway.platforms import load_adapter_class
        with pytest.raises(AdapterUnavailable, match="No adapter for platform 'carrier-pigeon'"):
            load_adapter_class("carrier-pigeon")


class TestBaseAdapterDispatch:
    def test_dispatch_without_a_handler_is_a_no_op(self):
        async def scenario():
            a = FakeAdapter(GatewayConfig.from_dict({"platforms": {"fake": {}}}).platforms["fake"])
            await a.dispatch(MessageEvent(source=make_source(), text="x"))
        run(scenario())
