"""Tools declared `async def` must actually run.

`registry.dispatch` is synchronous and used to hand the returned coroutine
straight to `json.dumps`, so every `is_async=True` tool failed with "Object of
type coroutine is not JSON serializable" — the four browser tools and
`send_message`. (Memory tools escaped it: the agent routes those through
`MemoryManager`, not dispatch.)
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gyrfalcon.model_tools import discover_builtin_tools
from gyrfalcon.tools import registry


@pytest.fixture(autouse=True)
def _tools():
    discover_builtin_tools()


def test_an_async_handler_is_run_not_returned_as_a_coroutine():
    async def handler(args, **kwargs):
        return json.dumps({"got": args.get("x")})

    registry.register(name="_probe_async", toolset="_probe", schema={"name": "_probe_async", "parameters": {}},
                      handler=handler, is_async=True)
    assert json.loads(registry.dispatch("_probe_async", {"x": 42}))["got"] == 42


def test_an_async_handler_returning_a_dict_is_serialised():
    async def handler(args, **kwargs):
        return {"ok": True}

    registry.register(name="_probe_dict", toolset="_probe", schema={"name": "_probe_dict", "parameters": {}},
                      handler=handler, is_async=True)
    assert json.loads(registry.dispatch("_probe_dict", {}))["ok"] is True


def test_an_async_handler_that_awaits_really_awaits():
    async def handler(args, **kwargs):
        await asyncio.sleep(0.01)
        return "done"

    registry.register(name="_probe_await", toolset="_probe", schema={"name": "_probe_await", "parameters": {}},
                      handler=handler, is_async=True)
    assert registry.dispatch("_probe_await", {}) == "done"


def test_an_async_handler_raising_is_reported_like_any_other():
    async def handler(args, **kwargs):
        raise ValueError("bad input")

    registry.register(name="_probe_raise", toolset="_probe", schema={"name": "_probe_raise", "parameters": {}},
                      handler=handler, is_async=True)
    assert "bad input" in json.loads(registry.dispatch("_probe_raise", {}))["error"]


def test_dispatch_works_from_inside_a_running_event_loop():
    """Blocking the caller's loop would deadlock, so the work goes elsewhere."""
    async def handler(args, **kwargs):
        await asyncio.sleep(0)
        return "from inside a loop"

    registry.register(name="_probe_in_loop", toolset="_probe", schema={"name": "_probe_in_loop", "parameters": {}},
                      handler=handler, is_async=True)

    async def scenario():
        # Called directly on the loop thread — `run_in_executor` would put it on
        # a worker where there is no running loop, exercising the other branch.
        return registry.dispatch("_probe_in_loop", {})

    assert asyncio.run(scenario()) == "from inside a loop"


def test_sync_handlers_are_unaffected():
    registry.register(name="_probe_sync", toolset="_probe", schema={"name": "_probe_sync", "parameters": {}},
                      handler=lambda args, **kw: "plain")
    assert registry.dispatch("_probe_sync", {}) == "plain"


@pytest.mark.parametrize("name", ["browser_navigate", "send_message"])
def test_real_async_tools_no_longer_return_a_serialisation_error(name):
    if registry.get_entry(name) is None:
        pytest.skip(f"{name} is not registered in this environment")
    out = registry.dispatch(name, {"text": "hi", "channel": "slack:C1", "url": "http://localhost:1"})
    assert "not JSON serializable" not in out


class TestSendMessage:
    """It reports what actually happened, rather than always claiming success."""

    def _call(self, **args):
        return json.loads(registry.dispatch("send_message", args))

    def test_without_a_gateway_it_says_so_instead_of_claiming_success(self):
        out = self._call(text="the report", channel="slack:C1")
        assert out["status"] == "failed" and "no gateway is running" in out["error"]

    def test_a_malformed_destination_is_explained(self):
        assert "not a destination" in self._call(text="hi", channel="C1")["error"]

    def test_a_missing_destination_is_explained(self):
        assert "No channel provided" in self._call(text="hi")["error"]

    def test_empty_text_is_refused(self):
        assert self._call(text="   ", channel="slack:C1")["error"] == "No text provided"

    def test_it_delivers_through_a_running_gateway(self):
        import sys
        sys.path.insert(0, "tests/gateway")
        from _fakes import FakeAdapter

        from gyrfalcon.gateway.config import PlatformConfig
        from gyrfalcon.gateway.delivery import DeliveryRouter, set_router

        async def scenario():
            adapter = FakeAdapter(PlatformConfig.from_config("slack", {}))
            set_router(DeliveryRouter({"slack": adapter}, asyncio.get_running_loop()))
            try:
                out = await asyncio.get_running_loop().run_in_executor(
                    None, registry.dispatch, "send_message", {"text": "done!", "channel": "slack:C0123"}
                )
                return json.loads(out), adapter
            finally:
                set_router(None)

        out, adapter = asyncio.run(scenario())
        assert out["status"] == "sent"
        assert adapter.sent == [{"chat_id": "C0123", "content": "done!", "reply_to": None}]

    def test_it_is_not_in_the_restricted_chat_toolset(self):
        # Posting to arbitrary channels is a wider capability than answering
        # where you were asked; a chat agent does not get it by default.
        from gyrfalcon.toolsets import resolve_toolset
        assert "send_message" not in resolve_toolset("gateway_safe")
