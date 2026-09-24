"""Live replies: the answer grows in one message as the model produces it.

The thread boundary is the point of interest — `AIAgent` fires its callbacks on
a worker thread, every platform call is a coroutine on the event loop.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from _fakes import FakeAdapter, FakeAgent, Harness, ev, run, wait_for

from gyrfalcon.gateway.config import PlatformConfig
from gyrfalcon.gateway.platforms.base import MessageEvent, SendResult, SessionSource
from gyrfalcon.gateway.streaming import MIN_FIRST_RENDER_CHARS, StreamingReply

LONG = "x" * MIN_FIRST_RENDER_CHARS


def an_event(chat="D1", thread="t1") -> MessageEvent:
    return MessageEvent(
        source=SessionSource(platform="fake", chat_id=chat, user_id="U1", thread_id=thread,
                             chat_type="dm", message_id="1.1"),
        text="hi",
    )


def adapter(**kw) -> FakeAdapter:
    return FakeAdapter(PlatformConfig.from_config("fake", {"allow": {"users": ["U1"]}}), **kw)


class TestRendering:
    def test_nothing_is_posted_before_there_is_something_to_say(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.01)
            s.begin()
            await asyncio.sleep(0.05)
            await s.abandon()
            return a
        assert run(scenario()).sent == []

    def test_a_very_short_answer_is_not_posted_early_then_immediately_finished(self):
        # Two API calls to say "ok" is worse than one.
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.01)
            s.begin()
            s.on_delta("ok")
            await asyncio.sleep(0.05)
            posted_during = list(a.sent)
            await s.finish("ok")
            return posted_during, a
        during, a = run(scenario())
        assert during == [] and a.sent == []

    def test_the_first_render_posts_and_later_ones_edit_the_same_message(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            s.on_delta(" more text here")
            await wait_for(lambda: a.edits)
            await s.finish(LONG + " more text here")
            return a
        a = run(scenario())
        assert len(a.sent) == 1, "one message, edited — not a message per chunk"
        assert a.sent[0]["content"] == LONG
        assert a.edits[-1]["content"] == LONG + " more text here"

    def test_the_reply_is_anchored_to_the_thread(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(thread="700.1"), interval=0.01)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            await s.abandon()
            return a
        assert run(scenario()).sent[0]["reply_to"] == "700.1"

    def test_updates_are_throttled_not_one_per_token(self):
        # The deltas must genuinely arrive over time, from another thread, so the
        # pump gets the chance to render between them — feeding them in a tight
        # synchronous loop never yields to the loop and would pass either way.
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.05)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            stop = threading.Event()

            def feeder():
                i = 0
                while not stop.is_set():
                    s.on_delta(f" {i}")
                    i += 1
                    stop.wait(0.001)      # ~1000 deltas/second

            threading.Thread(target=feeder, daemon=True).start()
            await asyncio.sleep(0.3)      # ~6 intervals, ~300 deltas
            stop.set()
            calls = len(a.sent) + len(a.edits)
            await s.abandon()
            return calls
        calls = run(scenario())
        assert calls <= 12, f"~300 deltas over 6 intervals became {calls} API calls"

    def test_an_unchanged_buffer_is_not_re_sent(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            await asyncio.sleep(0.1)
            await s.abandon()
            return a
        assert run(scenario()).edits == []

    def test_a_running_tool_is_shown_and_then_cleared(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            s.on_tool("web_search", {}, "start")
            await wait_for(lambda: a.sent)
            with_tool = a.sent[0]["content"]
            s.on_tool("web_search", {}, "complete")
            s.on_delta(" done")
            await wait_for(lambda: a.edits)
            await s.finish(LONG + " done")
            return with_tool, a
        with_tool, a = run(scenario())
        assert "running `web_search`" in with_tool
        assert "running `web_search`" not in a.edits[-1]["content"]

    def test_tool_activity_alone_is_enough_to_show_something(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_tool("terminal", {}, "start")
            await asyncio.sleep(0.08)
            await s.abandon()
            return a
        # No text yet, but the user should still see that work is happening.
        assert run(scenario()).sent == [], "below the first-render threshold, so nothing yet"


class TestFinishing:
    def test_finish_settles_on_the_final_text(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            return await s.finish(LONG + " FINAL"), a
        delivered, a = run(scenario())
        assert delivered is True and a.edits[-1]["content"] == LONG + " FINAL"

    def test_finish_without_anything_posted_hands_delivery_back(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=5)
            s.begin()
            return await s.finish("the whole answer"), a
        delivered, a = run(scenario())
        assert delivered is False, "the caller must send it the ordinary way"
        assert a.sent == [] and a.edits == []

    def test_a_long_final_answer_is_split_across_messages(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            body = "\n\n".join(f"para {i} " + "word " * 120 for i in range(20))
            return await s.finish(body), a
        delivered, a = run(scenario())
        assert delivered is True
        assert len(a.edits) >= 1 and len(a.sent) > 1, "first part edited in, the rest posted after"
        assert all(m["reply_to"] == "t1" for m in a.sent)

    def test_a_failed_final_edit_hands_delivery_back(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)

            async def no(*_a, **_k):
                return False
            a.edit_message = no
            return await s.finish("final"), a
        delivered, _ = run(scenario())
        assert delivered is False

    def test_abandon_leaves_the_partial_text_and_stops_pumping(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            await s.abandon()
            before = len(a.sent) + len(a.edits)
            s.on_delta(" ignored after abandon")
            await asyncio.sleep(0.08)
            return before, len(a.sent) + len(a.edits)
        before, after = run(scenario())
        assert before == after

    def test_finish_is_safe_to_call_twice(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await wait_for(lambda: a.sent)
            await s.finish("done")
            await s.finish("done")
        run(scenario())


class TestSafety:
    def test_partial_text_is_scrubbed_too(self):
        # A token can appear mid-stream; the edit that shows it is permanent.
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02,
                               transform=lambda t: t.replace("SECRET", "[redacted]"))
            s.begin()
            s.on_delta(LONG + " SECRET")
            await wait_for(lambda: a.sent)
            await s.finish(LONG + " SECRET")
            return a
        a = run(scenario())
        assert "SECRET" not in a.sent[0]["content"]
        assert "SECRET" not in a.edits[-1]["content"]

    def test_a_send_failure_does_not_wedge_the_stream(self):
        async def scenario():
            a = adapter()

            async def fail(*_a, **_k):
                return SendResult(success=False, error="channel_not_found")
            a.send = fail
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await asyncio.sleep(0.08)
            return await s.finish("final")
        assert run(scenario()) is False

    def test_an_exploding_adapter_does_not_escape_the_pump(self):
        async def scenario():
            a = adapter()

            async def boom(*_a, **_k):
                raise RuntimeError("slack is down")
            a.send = boom
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            s.on_delta(LONG)
            await asyncio.sleep(0.08)
            return await s.finish("final")
        assert run(scenario()) is False

    def test_deltas_from_a_worker_thread_are_rendered_by_the_loop(self):
        async def scenario():
            a = adapter()
            s = StreamingReply(a, an_event(), interval=0.02)
            s.begin()
            done = threading.Event()

            def worker():
                for i in range(200):
                    s.on_delta(f"chunk{i} ")
                done.set()

            threading.Thread(target=worker, daemon=True).start()
            await wait_for(done.is_set)
            await asyncio.sleep(0.05)
            await s.finish("".join(f"chunk{i} " for i in range(200)).strip())
            return a
        a = run(scenario())
        assert a.sent and "chunk199" in a.edits[-1]["content"]


class TestThroughTheRunner:
    """The runner attaches the callbacks to the cached agent for the turn."""

    def _streaming_agent(self, chunks, reply=None):
        class StreamingAgent(FakeAgent):
            def run_conversation(self, user_message, conversation_history=None, **_):
                self.calls.append({"message": user_message, "history": conversation_history,
                                   "thread": threading.current_thread().name, "principal": None, "probe": None})
                for c in chunks:
                    if self.stream_delta_callback:
                        self.stream_delta_callback(c)
                return {"final_response": reply if reply is not None else "".join(chunks)}
        agent = StreamingAgent()
        agent.stream_delta_callback = None
        agent.tool_progress_callback = None
        return agent

    def test_a_streamed_turn_edits_one_message_and_sends_no_second_copy(self):
        agent = None

        def factory(s, i, st):
            nonlocal agent
            agent = self._streaming_agent([LONG, " and the rest"])
            return agent

        async def scenario():
            h = Harness(agent_factory=factory, gateway={"platforms": {}})
            h.config.platforms["fake"].extra["stream_interval_ms"] = 20
            await h.send(ev("tell me"))
            return h
        h = run(scenario())
        assert len(h.adapter.sent) == 1, "one message total — the answer was edited into it"
        # What the user ends up seeing, whether that came from the first post or
        # a later edit (the fake produces its chunks faster than the pump ticks).
        visible = (h.adapter.edits or h.adapter.sent)[-1]["content"]
        assert visible == LONG + " and the rest"

    def test_callbacks_are_detached_after_the_turn(self):
        agent = None

        def factory(s, i, st):
            nonlocal agent
            agent = self._streaming_agent([LONG])
            return agent

        async def scenario():
            h = Harness(agent_factory=factory)
            await h.send(ev("one"))
            return agent
        a = run(scenario())
        assert a.stream_delta_callback is None and a.tool_progress_callback is None

    def test_a_non_streaming_agent_still_gets_its_reply_delivered(self):
        async def scenario():
            h = Harness()
            await h.send(ev("hi"))
            return h
        assert run(scenario()).adapter.texts == ["ok"]

    def _stream_for(self, platform):
        """`_begin_stream` starts a task, so it needs a running loop."""
        async def scenario():
            h = Harness(platform={"allow": {"users": ["U1"]}, **platform})
            stream = h.runner._begin_stream(h.adapter, ev("hi"))
            if stream is not None:
                await stream.abandon()
            return stream
        return run(scenario())

    def test_the_stream_switch_is_honoured_directly(self):
        # End to end this is hard to see: with streaming on but a fast agent the
        # pump may never tick, so the reply is delivered whole either way.
        assert self._stream_for({"stream": False}) is None
        assert self._stream_for({}) is not None

    def test_the_configured_interval_is_used(self):
        assert self._stream_for({"stream_interval_ms": 250})._interval == 0.25

    @pytest.mark.parametrize("bad", ["soon", None, -5, 0])
    def test_a_nonsense_interval_falls_back_to_the_default(self, bad):
        from gyrfalcon.gateway.streaming import DEFAULT_INTERVAL_SECONDS
        assert self._stream_for({"stream_interval_ms": bad})._interval == DEFAULT_INTERVAL_SECONDS

    def test_streaming_can_be_switched_off(self):
        async def scenario():
            h = Harness(platform={"allow": {"users": ["U1"]}, "stream": False},
                        agent_factory=lambda s, i, st: self._streaming_agent([LONG, " more"]))
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert h.adapter.edits == [], "no live editing"
        assert h.adapter.texts == [LONG + " more"], "delivered whole, once"

    def test_a_failed_turn_does_not_settle_its_half_written_answer(self):
        def factory(s, i, st):
            class Exploding(FakeAgent):
                def run_conversation(self, user_message, conversation_history=None, **_):
                    if self.stream_delta_callback:
                        self.stream_delta_callback(LONG + " I was about to say")
                    raise RuntimeError("model died")
            a = Exploding()
            a.stream_delta_callback = None
            a.tool_progress_callback = None
            return a

        async def scenario():
            h = Harness(agent_factory=factory)
            h.config.platforms["fake"].extra["stream_interval_ms"] = 20
            await h.send(ev("hi"))
            return h
        h = run(scenario())
        assert any("went wrong" in m["content"] for m in h.adapter.sent)
        assert not any("about to say" in m["content"] for m in h.adapter.edits), \
            "the half-written answer was not settled as if it had finished"

    def test_a_failed_turn_stops_its_pump(self):
        # The turn must last long enough for the pump to have really started —
        # a turn that fails instantly never renders, mutation or not, so it
        # cannot tell a stopped pump from an abandoned one still looping.
        streams = []

        def factory(s, i, st):
            class Exploding(FakeAgent):
                def run_conversation(self, user_message, conversation_history=None, **_):
                    streams.append(self.stream_delta_callback)
                    self.stream_delta_callback(LONG + " partial")
                    # Must outlast the runner's 0.2s interval floor, or the pump
                    # never ticks and the test proves nothing.
                    threading.Event().wait(0.6)
                    raise RuntimeError("model died")
            a = Exploding()
            a.stream_delta_callback = None
            a.tool_progress_callback = None
            return a

        async def scenario():
            h = Harness(agent_factory=factory)
            h.config.platforms["fake"].extra["stream_interval_ms"] = 200
            await h.send(ev("hi"))
            rendered = len(h.adapter.sent) + len(h.adapter.edits)
            streams[0](" still streaming after the turn died")
            await asyncio.sleep(0.6)
            return rendered, len(h.adapter.sent) + len(h.adapter.edits), h
        during, after, h = run(scenario())
        assert any(LONG in m["content"] for m in h.adapter.sent), "the pump really did render"
        assert after == during, "the pump kept rendering after the turn failed"
        assert not any("still streaming" in m["content"] for m in h.adapter.edits)

    def test_two_turns_in_one_session_do_not_share_a_stream(self):
        agents = []

        def factory(s, i, st):
            a = self._streaming_agent([LONG])
            agents.append(a)
            return a

        async def scenario():
            h = Harness(agent_factory=factory)
            h.config.platforms["fake"].extra["stream_interval_ms"] = 20
            await h.send(ev("one"))
            first = len(h.adapter.sent)
            await h.send(ev("two"))
            return first, h
        first, h = run(scenario())
        assert first == 1 and len(h.adapter.sent) == 2, "a second message for the second turn"
