"""Human-in-the-loop approval: a dangerous tool call stops and asks a person.

`tools/approval.py` has always decided *whether* a command needs approval;
nothing ever asked anyone — the refusal went back to the model, which could
rephrase and retry. These cover the half that makes it a boundary.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from _fakes import DEFAULT_PLATFORM, FakeAdapter, FakeAgent, Harness, ev, make_source, run, wait_for

from gyrfalcon.gateway.approval import (
    ApprovalBroker,
    ApprovalRequest,
    ask_for_approval,
    current_conversation,
    in_conversation,
    set_broker,
)
from gyrfalcon.gateway.config import AllowList, GatewayConfig
from gyrfalcon.gateway.platforms.base import ApprovalInteraction
from gyrfalcon.gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _no_leaked_broker():
    set_broker(None)
    yield
    set_broker(None)


def a_request(**kw) -> ApprovalRequest:
    base = dict(tool="terminal", reason="APPROVAL_REQUIRED: Elevated privileges",
                command="sudo systemctl restart nginx", session_key="fake:D1:t1")
    return ApprovalRequest(**{**base, **kw})


class TestBrokerMechanics:
    """A worker thread blocks; the event loop asks and answers."""

    def test_a_question_reaches_the_asker_and_the_answer_reaches_the_worker(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            asked = []

            async def asker(req):
                asked.append(req)
                return True

            broker.register_asker("fake:D1:t1", asker)
            decisions = []

            def worker():
                decisions.append(broker.request(a_request()))

            threading.Thread(target=worker, daemon=True).start()
            await wait_for(lambda: asked)
            broker.resolve(asked[0].id, True, by="U1")
            await wait_for(lambda: decisions)
            return asked[0], decisions[0]
        request, decision = run(scenario())
        assert request.tool == "terminal" and "sudo" in request.command
        assert decision.approved and decision.by == "U1"

    def test_a_denial_comes_back_as_a_denial(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            asked = []
            broker.register_asker("fake:D1:t1", lambda r: asked.append(r) or _true())
            out = []
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: asked)
            broker.resolve(asked[0].id, False, by="U1")
            await wait_for(lambda: out)
            return out[0]
        decision = run(scenario())
        assert not decision.approved and bool(decision) is False

    def test_nobody_answering_releases_the_worker_as_a_refusal(self):
        # Otherwise a forgotten prompt holds a worker thread, and a slot in the
        # turn pool, indefinitely.
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=0.15)
            broker.register_asker("fake:D1:t1", lambda r: _true())
            out = []
            started = time.monotonic()
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: out, timeout=5)
            return out[0], time.monotonic() - started
        decision, elapsed = run(scenario())
        assert not decision.approved and "nobody answered" in decision.detail
        assert elapsed < 3

    def test_with_nobody_to_ask_the_answer_is_no(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            out = []
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: out)
            return out[0]
        decision = run(scenario())
        assert not decision.approved and "nobody to ask" in decision.detail

    def test_an_asker_that_cannot_deliver_is_a_refusal(self):
        async def scenario():
            # A long timeout, so "refused because undeliverable" is clearly
            # distinguishable from "refused because nobody answered in time".
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=30)

            async def cannot(req):
                return False

            broker.register_asker("fake:D1:t1", cannot)
            out = []
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: out)
            return out[0], broker.pending_for("fake:D1:t1")
        decision, still_pending = run(scenario())
        assert not decision.approved and still_pending == []
        assert "could not be delivered" in decision.detail

    def test_an_asker_that_explodes_is_a_refusal(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)

            async def boom(req):
                raise RuntimeError("slack is down")

            broker.register_asker("fake:D1:t1", boom)
            out = []
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: out)
            return out[0]
        assert not run(scenario()).approved

    def test_answering_twice_only_counts_once(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            asked = []
            broker.register_asker("fake:D1:t1", lambda r: asked.append(r) or _true())
            out = []
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: asked)
            first = broker.resolve(asked[0].id, True, by="U1")
            second = broker.resolve(asked[0].id, False, by="U2")
            await wait_for(lambda: out)
            return first, second, out[0]
        first, second, decision = run(scenario())
        assert first is not None and second is None, "the second click resolves nothing"
        assert decision.approved and decision.by == "U1"

    def test_an_unknown_request_id_resolves_nothing(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            return broker.resolve("nonexistent", True, by="U1")
        assert run(scenario()) is None

    def test_cancelling_a_conversation_refuses_its_questions(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            asked = []
            broker.register_asker("fake:D1:t1", lambda r: asked.append(r) or _true())
            out = []
            threading.Thread(target=lambda: out.append(broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: asked)
            n = broker.cancel_for("fake:D1:t1", detail="you stopped it")
            await wait_for(lambda: out)
            return n, out[0]
        n, decision = run(scenario())
        assert n == 1 and not decision.approved and "stopped" in decision.detail

    def test_cancelling_leaves_other_conversations_alone(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            for key in ("fake:D1:t1", "fake:D2:t2"):
                broker.register_asker(key, lambda r: _true())
            out = []
            for key in ("fake:D1:t1", "fake:D2:t2"):
                threading.Thread(target=lambda k=key: out.append(broker.request(a_request(session_key=k))),
                                 daemon=True).start()
            await wait_for(lambda: len(broker.pending_for("fake:D1:t1")) + len(broker.pending_for("fake:D2:t2")) == 2)
            broker.cancel_for("fake:D1:t1")
            await wait_for(lambda: out)
            return broker.pending_for("fake:D2:t2")
        assert len(run(scenario())) == 1

    def test_resolve_latest_answers_the_newest_open_question(self):
        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            broker.register_asker("fake:D1:t1", lambda r: _true())
            out = []
            for i in range(2):
                threading.Thread(
                    target=lambda i=i: out.append(broker.request(a_request(command=f"cmd{i}"))), daemon=True).start()
                await wait_for(lambda: len(broker.pending_for("fake:D1:t1")) == i + 1)
            newest = max(broker.pending_for("fake:D1:t1"), key=lambda r: r.created_at)
            answered = broker.resolve_latest("fake:D1:t1", True, by="U1")
            await wait_for(lambda: out)
            return answered, newest
        answered, newest = run(scenario())
        assert answered.id == newest.id

    def test_resolve_latest_with_nothing_open(self):
        async def scenario():
            return ApprovalBroker(asyncio.get_running_loop()).resolve_latest("fake:D1:t1", True)
        assert run(scenario()) is None


def _true():
    async def done():
        return True
    return done()


class TestContextPlumbing:
    def test_a_tool_deep_in_a_turn_finds_its_conversation(self):
        assert current_conversation() == ""
        with in_conversation("fake:D1:t1"):
            assert current_conversation() == "fake:D1:t1"
        assert current_conversation() == ""

    def test_without_a_gateway_nobody_is_asked(self):
        # The CLI, TUI, dashboard and scheduler keep their old behaviour.
        with in_conversation("fake:D1:t1"):
            assert ask_for_approval("terminal", "APPROVAL_REQUIRED: x", "sudo x") is None

    def test_with_a_broker_but_no_conversation_nobody_is_asked(self):
        async def scenario():
            set_broker(ApprovalBroker(asyncio.get_running_loop()))
            return ask_for_approval("terminal", "r", "c")
        assert run(scenario()) is None


class TestDispatchIntegration:
    """`handle_function_call` is where the request is intercepted."""

    def _tool_needing_approval(self):
        return json.dumps({"approval_required": True, "reason": "APPROVAL_REQUIRED: Elevated privileges",
                           "command": "sudo reboot"})

    def test_an_approved_call_is_re_run_with_its_original_arguments(self):
        from unittest.mock import patch

        from gyrfalcon.model_tools import handle_function_call

        calls = []

        def dispatch(name, args, **kw):
            calls.append(args)
            return self._tool_needing_approval() if len(calls) == 1 else json.dumps({"stdout": "done"})

        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            asked = []
            broker.register_asker("k", lambda r: asked.append(r) or _true())
            set_broker(broker)
            out = []

            def worker():
                with in_conversation("k"):
                    out.append(handle_function_call("terminal", {"command": "sudo reboot", "workdir": "/srv"}))

            threading.Thread(target=worker, daemon=True).start()
            await wait_for(lambda: asked)
            broker.resolve(asked[0].id, True, by="U1")
            await wait_for(lambda: out)
            return out[0]

        with patch("gyrfalcon.tools.registry.dispatch", dispatch):
            result = run(scenario())
        assert json.loads(result) == {"stdout": "done"}
        assert calls == [{"command": "sudo reboot", "workdir": "/srv"}] * 2, \
            "re-run with the full original arguments, not a payload rebuild"

    def test_a_denied_call_never_runs_and_the_model_is_told(self):
        from unittest.mock import patch

        from gyrfalcon.model_tools import handle_function_call

        calls = []

        def dispatch(name, args, **kw):
            calls.append(args)
            return self._tool_needing_approval()

        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            asked = []
            broker.register_asker("k", lambda r: asked.append(r) or _true())
            set_broker(broker)
            out = []
            def worker():
                with in_conversation("k"):
                    out.append(handle_function_call("terminal", {"command": "sudo reboot"}))

            threading.Thread(target=worker, daemon=True).start()
            await wait_for(lambda: asked)
            broker.resolve(asked[0].id, False, by="U1")
            await wait_for(lambda: out)
            return out[0]

        with patch("gyrfalcon.tools.registry.dispatch", dispatch):
            result = run(scenario())
        assert len(calls) == 1, "the tool ran once to report, and never for real"
        payload = json.loads(result)
        assert payload["denied"] is True and "U1" in payload["error"]

    def test_with_no_gateway_the_payload_still_goes_to_the_model(self):
        from unittest.mock import patch

        from gyrfalcon.model_tools import handle_function_call

        with patch("gyrfalcon.tools.registry.dispatch", lambda n, a, **k: self._tool_needing_approval()):
            result = handle_function_call("terminal", {"command": "sudo reboot"})
        assert json.loads(result)["approval_required"] is True

    def test_an_ordinary_result_is_untouched(self):
        from unittest.mock import patch

        from gyrfalcon.model_tools import handle_function_call

        with patch("gyrfalcon.tools.registry.dispatch", lambda n, a, **k: json.dumps({"stdout": "hi"})):
            assert json.loads(handle_function_call("terminal", {"command": "ls"})) == {"stdout": "hi"}

    def test_a_hardline_blocked_command_is_never_askable(self):
        # BLOCKED commands return {"error": ...}, not approval_required, so no
        # question is ever put to anyone — approval cannot unlock `rm -rf /`.
        from gyrfalcon.tools.terminal_tool import terminal_tool
        out = json.loads(terminal_tool({"command": "rm -rf /"}))
        assert "BLOCKED" in out["error"] and "approval_required" not in out


class TestWhoMayDecide:
    """A button in a channel can be clicked by anyone who can see it."""

    def _rig(self, platform=None):
        config = GatewayConfig.from_dict({"platforms": {"fake": platform or DEFAULT_PLATFORM}})
        runner = GatewayRunner(config, session_db=type("DB", (), {"close": lambda self: None})(),
                               run_scheduler=False, adapter_loader=lambda n: (lambda c: FakeAdapter(c)))
        adapter = FakeAdapter(config.platforms["fake"])
        return runner, adapter

    def _pending(self, runner, key="fake:D1:t1"):
        async def asker(req):
            return True
        runner._broker.register_asker(key, asker)
        out = []
        threading.Thread(target=lambda: out.append(runner._broker.request(a_request(session_key=key))),
                         daemon=True).start()
        return out

    def test_an_allowlisted_user_may_approve(self):
        async def scenario():
            runner, adapter = self._rig()
            runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            out = self._pending(runner)
            await wait_for(lambda: runner._broker.pending_for("fake:D1:t1"))
            req = runner._broker.pending_for("fake:D1:t1")[0]
            await runner.handle_decision(adapter, ApprovalInteraction(req.id, True, "U1", "D1", "fake"))
            await wait_for(lambda: out)
            return out[0], adapter
        decision, adapter = run(scenario())
        assert decision.approved and decision.by == "U1"
        assert any("Approved" in c for c in adapter.settled.values())

    def test_a_stranger_clicking_the_button_decides_nothing(self):
        async def scenario():
            runner, adapter = self._rig()
            runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=0.4)
            out = self._pending(runner)
            await wait_for(lambda: runner._broker.pending_for("fake:D1:t1"))
            req = runner._broker.pending_for("fake:D1:t1")[0]
            await runner.handle_decision(adapter, ApprovalInteraction(req.id, True, "USTRANGER", "D1", "fake"))
            still_open = bool(runner._broker.pending_for("fake:D1:t1"))
            await wait_for(lambda: out, timeout=5)
            return still_open, out[0]
        still_open, decision = run(scenario())
        assert still_open, "the stranger's click was ignored"
        assert not decision.approved, "and it timed out as a refusal"

    def test_a_stale_button_is_reported_not_silently_ignored(self):
        async def scenario():
            runner, adapter = self._rig()
            runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            await runner.handle_decision(adapter, ApprovalInteraction("gone", True, "U1", "D1", "fake"))
            return adapter
        adapter = run(scenario())
        assert "no longer open" in adapter.settled["gone"]

    def test_elevation_requires_being_on_the_allowlist_first(self):
        allow = AllowList.from_config({"users": ["U1"], "elevated": ["U1", "USTRANGER"]})
        assert allow.is_elevated("U1")
        assert not allow.is_elevated("USTRANGER"), "elevation is not a way in"
        assert not allow.permits("USTRANGER", "D1", "dm")


class TestElevatedUsers:
    def _settings(self, platform, user="U1"):
        h = Harness(platform=platform)
        return h.runner.resolve_settings(make_source(user=user))

    def test_an_ordinary_user_gets_the_restricted_set(self):
        s = self._settings({"allow": {"users": ["U1", "U2"], "elevated": ["U2"]}}, user="U1")
        assert s.toolset == "gateway_safe"

    def test_an_elevated_user_gets_the_wider_set(self):
        s = self._settings({"allow": {"users": ["U1", "U2"], "elevated": ["U2"]}}, user="U2")
        assert s.toolset == "core"

    def test_the_elevated_toolset_is_configurable(self):
        s = self._settings({"allow": {"users": ["U1"], "elevated": ["U1"]}, "elevated_toolset": "web"}, user="U1")
        assert s.toolset == "web"

    def test_with_nobody_elevated_everyone_is_restricted(self):
        assert self._settings({"allow": {"users": ["U1"]}}).toolset == "gateway_safe"

    def test_an_elevated_agent_is_still_restricted_at_dispatch(self):
        # "Wider" is not "unbounded": the ceiling still applies, and every
        # dangerous call in it still has to be approved.
        from unittest.mock import patch

        built = {}

        class FakeAIAgent:
            def __init__(self, **kw):
                built.update(kw)

        h = Harness(platform={"allow": {"users": ["U1"], "elevated": ["U1"]}})
        src = make_source(user="U1")
        with patch("gyrfalcon.run_agent.AIAgent", FakeAIAgent), \
             patch("gyrfalcon.providers.copilot.is_authenticated", lambda: False):
            h.runner._build_agent(src, None, h.runner.resolve_settings(src))
        assert built["enabled_toolsets"] == ["core"] and built["restrict_tools"] is True


class TestControlCommands:
    def test_approve_with_nothing_pending(self):
        async def scenario():
            h = Harness()
            await h.send(ev("!approve"))
            return h
        assert "Nothing is waiting" in run(scenario()).adapter.texts[0]

    def test_approve_answers_the_pending_question(self):
        async def scenario():
            h = Harness()
            h.runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            h.runner._broker.register_asker("fake:D1:t1", lambda r: _true())
            out = []
            threading.Thread(
                target=lambda: out.append(h.runner._broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: h.runner._broker.pending_for("fake:D1:t1"))
            await h.send(ev("!approve"))
            await wait_for(lambda: out)
            return out[0], h
        decision, h = run(scenario())
        assert decision.approved and decision.by == "U1"
        assert any("Approved" in t for t in h.adapter.texts)

    def test_deny_answers_the_pending_question(self):
        async def scenario():
            h = Harness()
            h.runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=5)
            h.runner._broker.register_asker("fake:D1:t1", lambda r: _true())
            out = []
            threading.Thread(
                target=lambda: out.append(h.runner._broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: h.runner._broker.pending_for("fake:D1:t1"))
            await h.send(ev("!deny"))
            await wait_for(lambda: out)
            return out[0]
        assert not run(scenario()).approved

    def test_help_mentions_them(self):
        async def scenario():
            h = Harness()
            await h.send(ev("!help"))
            return h
        assert "approve" in run(scenario()).adapter.texts[0]

    def test_stop_releases_a_waiting_question(self):
        async def scenario():
            h = Harness()
            h.runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=10)
            h.runner._broker.register_asker("fake:D1:t1", lambda r: _true())
            out = []
            threading.Thread(
                target=lambda: out.append(h.runner._broker.request(a_request())), daemon=True).start()
            await wait_for(lambda: h.runner._broker.pending_for("fake:D1:t1"))
            from gyrfalcon.gateway.run import _SessionState
            h.runner._states["fake:D1:t1"] = _SessionState(busy=True)
            await h.send(ev("!stop"))
            await wait_for(lambda: out)
            return out[0]
        decision = run(scenario())
        assert not decision.approved and "stopped" in decision.detail


class TestShutdown:
    def test_stopping_the_gateway_releases_paused_turns(self):
        async def scenario():
            config = GatewayConfig.from_dict({"platforms": {}})
            runner = GatewayRunner(config, session_db=type("DB", (), {"close": lambda self: None})(),
                                   run_scheduler=False)
            task = asyncio.create_task(runner.start())
            await wait_for(lambda: runner._broker is not None)
            runner._broker.register_asker("k", lambda r: _true())
            out = []
            threading.Thread(target=lambda: out.append(runner._broker.request(a_request(session_key="k"))),
                             daemon=True).start()
            await wait_for(lambda: runner._broker.pending_for("k"))
            await asyncio.wait_for(runner.stop(), 5)
            await asyncio.wait_for(task, 2)
            await wait_for(lambda: out)
            return out[0]
        decision = run(scenario())
        assert not decision.approved and "shutting down" in decision.detail


class TestTheWholeChainForReal:
    """No patching of the tool layer: the real `terminal_tool`, the real
    dangerous-command detector, the real grant, and a command that is genuinely
    executed once approved. `chmod 777` matches a DANGEROUS pattern and is
    harmless against a temp file, so the file's mode is the proof."""

    def test_an_approved_command_actually_runs_and_is_not_asked_twice(self, tmp_path):
        import os
        import stat

        from gyrfalcon.model_tools import handle_function_call

        probe = tmp_path / "probe.txt"
        probe.write_text("x")
        os.chmod(probe, 0o600)
        command = f"chmod 777 {probe}"

        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=10)
            asked = []
            broker.register_asker("k", lambda r: asked.append(r) or _true())
            set_broker(broker)
            out = []

            def worker():
                with in_conversation("k"):
                    out.append(handle_function_call("terminal", {"command": command}))

            threading.Thread(target=worker, daemon=True).start()
            await wait_for(lambda: asked, timeout=10)
            broker.resolve(asked[0].id, True, by="U1")
            await wait_for(lambda: out, timeout=10)
            return asked, out[0]

        asked, result = run(scenario(), timeout=30)
        assert len(asked) == 1, "asked once, not again on the approved re-run"
        assert "approval_required" not in result
        assert stat.S_IMODE(os.stat(probe).st_mode) == 0o777, "the command really ran"

    def test_a_denied_command_does_not_run(self, tmp_path):
        import os
        import stat

        from gyrfalcon.model_tools import handle_function_call

        probe = tmp_path / "probe.txt"
        probe.write_text("x")
        os.chmod(probe, 0o600)

        async def scenario():
            broker = ApprovalBroker(asyncio.get_running_loop(), timeout=10)
            asked = []
            broker.register_asker("k", lambda r: asked.append(r) or _true())
            set_broker(broker)
            out = []

            def worker():
                with in_conversation("k"):
                    out.append(handle_function_call("terminal", {"command": f"chmod 777 {probe}"}))

            threading.Thread(target=worker, daemon=True).start()
            await wait_for(lambda: asked, timeout=10)
            broker.resolve(asked[0].id, False, by="U1")
            await wait_for(lambda: out, timeout=10)
            return out[0]

        result = run(scenario(), timeout=30)
        assert json.loads(result)["denied"] is True
        assert stat.S_IMODE(os.stat(probe).st_mode) == 0o600, "the file was untouched"

    def test_the_grant_does_not_leak_to_a_later_call(self, tmp_path):
        """One approval authorises exactly one run."""
        from gyrfalcon.tools.approval import approved_for_this_call, needs_approval_for_tool

        command = "chmod 777 /tmp/whatever"
        with approved_for_this_call(command):
            assert needs_approval_for_tool("terminal", command)[0] is False
        assert needs_approval_for_tool("terminal", command)[0] is True

    def test_the_grant_is_confined_to_its_own_thread(self):
        from gyrfalcon.tools.approval import approved_for_this_call, needs_approval_for_tool

        command = "chmod 777 /tmp/whatever"
        seen = []
        with approved_for_this_call(command):
            t = threading.Thread(target=lambda: seen.append(needs_approval_for_tool("terminal", command)[0]))
            t.start()
            t.join()
        assert seen == [True], "another turn's approval must not authorise this one"


class TestQuestionsDoNotOutliveTheirTurn:
    """A question answered after its turn has ended would resume nothing, and
    would leave the next turn's `!approve` pointing at a dead request."""

    def test_a_question_still_open_when_the_turn_ends_is_released(self):
        lingering = []

        def factory(src, sid, settings):
            class Asking(FakeAgent):
                def run_conversation(self, user_message, conversation_history=None, **_):
                    from gyrfalcon.gateway.approval import get_broker
                    broker = get_broker()
                    started = threading.Event()
                    # Read here, on the turn's own thread: a bare Thread does
                    # not inherit contextvars, so reading it inside the closure
                    # below would find nothing.
                    key = current_conversation()

                    def ask_in_the_background():
                        started.set()
                        lingering.append(broker.request(a_request(session_key=key)))

                    threading.Thread(target=ask_in_the_background, daemon=True).start()
                    started.wait(2)
                    time.sleep(0.15)          # let the question register
                    return {"final_response": "done without waiting"}
            return Asking()

        async def scenario():
            h = Harness(agent_factory=factory)
            h.runner._broker = ApprovalBroker(asyncio.get_running_loop(), timeout=30)
            set_broker(h.runner._broker)
            await h.send(ev("go"))
            await wait_for(lambda: lingering, timeout=10)
            return lingering[0], h.runner._broker.pending_for("fake:D1:t1")

        decision, still_pending = run(scenario(), timeout=30)
        assert not decision.approved and "turn ended" in decision.detail
        assert still_pending == [], "and nothing is left registered"
