"""The Slack adapter over the real wire protocol, against a fake Slack.

The other adapter tests fake slack_sdk itself, which proves the adapter matches
*our model* of the SDK — and if that model is wrong, they agree with the bug.
Here the real `SocketModeClient` and `AsyncWebClient` talk to a local server that
speaks Slack's protocol: the `apps.connections.open` handshake, a websocket that
sends `hello` and event envelopes and expects acknowledgements back, and Web API
methods that record what they were sent. Only Slack's own servers are faked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from _fakes import FakeAgent, FakeSessionDB, run, wait_for

# The Slack SDK is an optional extra (`uv sync --extra slack`), so these skip
# rather than break collection for an install that does not use Slack — the same
# courtesy the PostgreSQL tests extend. Without this, a plain `uv run pytest`
# (which prunes extras) fails the entire suite at import.
pytest.importorskip("slack_sdk", reason="install with: uv sync --extra slack")
pytest.importorskip("aiohttp", reason="install with: uv sync --extra slack")

from aiohttp import WSMsgType, web  # noqa: E402
from slack_sdk.web.async_client import AsyncWebClient  # noqa: E402

from gyrfalcon.gateway.config import GatewayConfig, PlatformConfig  # noqa: E402
from gyrfalcon.gateway.platforms.slack import SlackAdapter  # noqa: E402
from gyrfalcon.gateway.run import GatewayRunner  # noqa: E402

BOT = "UBOT0001"
BOT_TOKEN = "xoxb-fixture-only"
APP_TOKEN = "xapp-1-A0123-3333333333-0123456789abcdef"


class FakeSlack:
    """Enough of Slack to connect to: Web API over HTTP, Socket Mode over a websocket."""

    def __init__(self):
        self.api: list[tuple[str, dict, dict]] = []
        self.received: list[dict] = []
        self.connections = 0
        self._sockets: list[web.WebSocketResponse] = []
        self._posts = 0
        self.fail: dict[str, str] = {}

    async def start(self) -> "FakeSlack":
        app = web.Application()
        app.router.add_route("*", "/api/{method}", self._api)  # the SDK uses GET for some methods
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for ws in self._sockets:
            await ws.close()
        await self._runner.cleanup()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/"

    def calls(self, method: str) -> list[dict]:
        return [body for m, body, _ in self.api if m == method]

    async def _api(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        body: Any = await request.json() if request.content_type == "application/json" else dict(await request.post())
        # Some methods (reactions.*) carry their arguments in the query string; Slack accepts either.
        body = {**dict(request.query), **body}
        self.api.append((method, body, dict(request.headers)))
        if method in self.fail:
            return web.json_response({"ok": False, "error": self.fail[method]})
        if method == "auth.test":
            return web.json_response({"ok": True, "user_id": BOT, "team_id": "T1", "user": "gyrfalcon", "bot_id": "B1"})
        if method == "apps.connections.open":
            return web.json_response({"ok": True, "url": f"ws://127.0.0.1:{self.port}/ws"})
        if method == "chat.postMessage":
            self._posts += 1
            return web.json_response({"ok": True, "ts": f"9000.{self._posts:04d}", "channel": body.get("channel")})
        if method == "conversations.info":
            return web.json_response({"ok": True, "channel": {"name": "ops", "is_im": False}})
        return web.json_response({"ok": True})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self._sockets.append(ws)
        await ws.send_json({"type": "hello", "num_connections": 1, "connection_info": {"app_id": "A1"}})
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                self.received.append(json.loads(msg.data))
        return ws

    async def push_event(self, event: dict, envelope_id: str = "env-1") -> None:
        envelope = {
            "envelope_id": envelope_id, "type": "events_api", "accepts_response_payload": False,
            "retry_attempt": 0, "retry_reason": "",
            "payload": {"team_id": "T1", "event_id": f"Ev-{envelope_id}", "type": "event_callback", "event": event},
        }
        await self._sockets[-1].send_json(envelope)

    async def push_disconnect(self) -> None:
        await self._sockets[-1].send_json({"type": "disconnect", "reason": "refresh_requested"})


def dm(text="hello", ts="1000.0001", user="U1", **kw) -> dict:
    return {"type": "message", "user": user, "channel": "D1", "channel_type": "im", "ts": ts, "text": text, **kw}


def platform() -> PlatformConfig:
    return PlatformConfig.from_config("slack", {"allow": {"users": ["U1"], "channels": ["C1"]}})


class Rig:
    """A fake Slack, a real adapter connected to it, and (optionally) a real runner."""

    def __init__(self, with_runner: bool = False, agent_factory=None):
        self.with_runner = with_runner
        self.agent_factory = agent_factory or (lambda *a: FakeAgent(reply="**ok**"))

    async def __aenter__(self):
        self.slack = await FakeSlack().start()
        web_client = AsyncWebClient(token=BOT_TOKEN, base_url=self.slack.base_url)
        self.adapter = SlackAdapter(platform(), web_client=web_client)
        self.adapter._resolve_token = lambda kind: {"bot": BOT_TOKEN, "app": APP_TOKEN}[kind]
        self.events: list = []
        if self.with_runner:
            config = GatewayConfig.from_dict({"platforms": {"slack": {"allow": {"users": ["U1"], "channels": ["C1"]}}}})
            self.runner = GatewayRunner(config, agent_factory=self.agent_factory, session_db=FakeSessionDB(),
                                        run_scheduler=False, adapter_loader=lambda n: (lambda c: self.adapter))
            self.runner._create_adapter("slack")
        else:
            async def collect(e):
                self.events.append(e)
            self.adapter.on_message(collect)
        assert await self.adapter.connect() is True
        return self

    async def __aexit__(self, *exc):
        await self.adapter.disconnect()
        await self.slack.stop()


class TestHandshake:
    def test_the_real_client_connects_through_apps_connections_open(self):
        async def scenario():
            async with Rig() as rig:
                await wait_for(lambda: rig.slack.connections == 1)
                return rig.slack.api
        api = run(scenario())
        methods = [m for m, _, _ in api]
        assert methods[0] == "auth.test" and "apps.connections.open" in methods
        _, _, open_headers = next(c for c in api if c[0] == "apps.connections.open")
        assert open_headers["Authorization"] == f"Bearer {APP_TOKEN}", "the app-level token opens the socket"
        _, _, auth_headers = next(c for c in api if c[0] == "auth.test")
        assert auth_headers["Authorization"] == f"Bearer {BOT_TOKEN}", "the bot token does everything else"

    def test_a_rejected_token_fails_connect_cleanly(self):
        async def scenario():
            slack = await FakeSlack().start()
            slack.fail["auth.test"] = "invalid_auth"
            try:
                adapter = SlackAdapter(platform(), web_client=AsyncWebClient(token=BOT_TOKEN, base_url=slack.base_url))
                adapter._resolve_token = lambda kind: {"bot": BOT_TOKEN, "app": APP_TOKEN}[kind]
                return await adapter.connect(), slack.connections
            finally:
                await slack.stop()
        ok, connections = run(scenario())
        assert ok is False and connections == 0, "no websocket is opened for a bad token"


class TestEvents:
    def test_an_event_is_acknowledged_on_the_wire_and_dispatched(self):
        async def scenario():
            async with Rig() as rig:
                await rig.slack.push_event(dm("hello there"), "env-42")
                await wait_for(lambda: rig.events)
                await wait_for(lambda: rig.slack.received)
                return rig.events, rig.slack.received
        events, received = run(scenario())
        assert received == [{"envelope_id": "env-42"}]
        assert events[0].text == "hello there" and events[0].event_id == "D1:1000.0001"

    def test_the_ack_goes_out_while_a_long_turn_is_still_running(self):
        # The point of ack-first: Slack redelivers anything unacknowledged for 3s.
        async def scenario():
            gate = asyncio.Event()
            async with Rig() as rig:
                async def slow(e):
                    await gate.wait()
                rig.adapter.on_message(slow)
                await rig.slack.push_event(dm(), "env-slow")
                await wait_for(lambda: rig.slack.received)
                acked_while_running = list(rig.slack.received)
                gate.set()
                return acked_while_running
        assert run(scenario()) == [{"envelope_id": "env-slow"}]

    def test_several_events_are_each_acknowledged(self):
        async def scenario():
            async with Rig() as rig:
                for i in range(5):
                    await rig.slack.push_event(dm(f"m{i}", ts=f"{i}.0"), f"env-{i}")
                await wait_for(lambda: len(rig.slack.received) == 5)
                await wait_for(lambda: len(rig.events) == 5)
                return sorted(m["envelope_id"] for m in rig.slack.received)
        assert run(scenario()) == [f"env-{i}" for i in range(5)]

    def test_the_bots_own_echo_is_acknowledged_but_never_dispatched(self):
        async def scenario():
            async with Rig() as rig:
                await rig.slack.push_event(dm("echo", user=BOT, bot_id="B1"), "env-echo")
                await wait_for(lambda: rig.slack.received)
                await asyncio.sleep(0.05)
                return rig.events, rig.slack.received
        events, received = run(scenario())
        assert events == [] and received == [{"envelope_id": "env-echo"}]

    def test_the_connection_survives_slack_asking_it_to_reconnect(self):
        # Slack recycles socket connections regularly with a `disconnect` message.
        async def scenario():
            async with Rig() as rig:
                await wait_for(lambda: rig.slack.connections == 1)
                await rig.slack.push_disconnect()
                await wait_for(lambda: rig.slack.connections == 2, timeout=10)
                await rig.slack.push_event(dm("after reconnect"), "env-after")
                await wait_for(lambda: rig.events)
                return rig.events[0].text
        assert run(scenario(), timeout=30) == "after reconnect"


class TestOutbound:
    def test_send_reaches_the_web_api_with_the_right_fields(self):
        async def scenario():
            async with Rig() as rig:
                r = await rig.adapter.send("C1", "**hi** [docs](https://x.io) <!channel>", reply_to="700.1")
                return r, rig.slack.calls("chat.postMessage")
        r, posts = run(scenario())
        (post,) = posts
        assert r.success and r.message_id == "9000.0001"
        assert post["channel"] == "C1" and post["thread_ts"] == "700.1"
        assert post["text"] == "*hi* <https://x.io|docs> &lt;!channel&gt;"
        assert post["mrkdwn"] is True and post["parse"] == "none" and post["link_names"] is False

    def test_a_top_level_dm_reply_omits_thread_ts_on_the_wire(self):
        # None must not be sent as the string "None" — that would be a bad thread_ts.
        async def scenario():
            async with Rig() as rig:
                from gyrfalcon.gateway.platforms.base import SessionSource
                src = SessionSource(platform="slack", chat_id="D1", user_id="U1", chat_type="dm", message_id="1.1")
                await rig.adapter.send("D1", "hi", reply_to="1.1", metadata={"source": src})
                return rig.slack.calls("chat.postMessage")[0]
        assert run(scenario()).get("thread_ts") in (None, "")

    def test_a_slack_error_becomes_a_failed_result_with_the_code(self):
        async def scenario():
            async with Rig() as rig:
                rig.slack.fail["chat.postMessage"] = "channel_not_found"
                return await rig.adapter.send("C9", "hi")
        r = run(scenario())
        assert r.success is False and r.error == "channel_not_found"

    def test_edit_and_reactions_use_the_documented_methods(self):
        async def scenario():
            async with Rig() as rig:
                from gyrfalcon.gateway.platforms.base import DONE, WORKING, MessageEvent, SessionSource
                e = MessageEvent(source=SessionSource(platform="slack", chat_id="D1", message_id="5.5"), text="x")
                await rig.adapter.edit_message("D1", "5.5", "**new**")
                await rig.adapter.acknowledge(e, WORKING)
                await rig.adapter.acknowledge(e, DONE)
                return rig.slack
        slack = run(scenario())
        assert slack.calls("chat.update")[0]["text"] == "*new*"
        assert [c["name"] for c in slack.calls("reactions.add")] == ["hourglass_flowing_sand", "white_check_mark"]
        assert [c["name"] for c in slack.calls("reactions.remove")] == ["hourglass_flowing_sand"]
        assert all(c["timestamp"] == "5.5" and c["channel"] == "D1" for c in slack.calls("reactions.add"))


class TestWholePath:
    """Fake Slack -> real socket client -> real adapter -> real runner -> fake agent -> real Web client -> Slack."""

    def test_a_dm_round_trips_with_progress_reactions(self):
        async def scenario():
            async with Rig(with_runner=True) as rig:
                await rig.slack.push_event(dm("what is up?"), "env-1")
                await wait_for(lambda: rig.slack.calls("chat.postMessage"))
                await wait_for(lambda: any(c["name"] == "white_check_mark" for c in rig.slack.calls("reactions.add")))
                return rig.slack
        slack = run(scenario())
        assert [r["text"] for r in slack.calls("chat.postMessage")] == ["*ok*"]
        assert [r["name"] for r in slack.calls("reactions.add")] == ["hourglass_flowing_sand", "white_check_mark"]

    def test_a_stranger_is_acknowledged_to_slack_but_gets_no_answer(self):
        built = []

        async def scenario():
            async with Rig(with_runner=True, agent_factory=lambda *a: built.append(a) or FakeAgent()) as rig:
                await rig.slack.push_event(dm("let me in", user="USTRANGER"), "env-x")
                await wait_for(lambda: rig.slack.received)
                await asyncio.sleep(0.1)
                return rig.slack
        slack = run(scenario())
        assert slack.received == [{"envelope_id": "env-x"}], "Slack is told we got it"
        assert built == [] and slack.calls("chat.postMessage") == []

    def test_a_channel_mention_is_answered_in_thread_and_the_follow_up_needs_no_mention(self):
        async def scenario():
            async with Rig(with_runner=True) as rig:
                await rig.slack.push_event({
                    "type": "app_mention", "user": "U1", "channel": "C1", "ts": "700.1",
                    "text": f"<@{BOT}> first"}, "env-1")
                await wait_for(lambda: len(rig.slack.calls("chat.postMessage")) == 1)
                await rig.slack.push_event({
                    "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel",
                    "ts": "701.1", "thread_ts": "700.1", "text": "second"}, "env-2")
                await wait_for(lambda: len(rig.slack.calls("chat.postMessage")) == 2)
                return rig.slack
        posts = run(scenario()).calls("chat.postMessage")
        assert [p["thread_ts"] for p in posts] == ["700.1", "700.1"]

    def test_the_double_delivery_of_a_mention_answers_once(self):
        async def scenario():
            async with Rig(with_runner=True) as rig:
                common = {"user": "U1", "channel": "C1", "channel_type": "channel", "ts": "800.1",
                          "text": f"<@{BOT}> once please"}
                await rig.slack.push_event({"type": "message", **common}, "env-a")
                await rig.slack.push_event({"type": "app_mention", **common}, "env-b")
                await wait_for(lambda: rig.slack.calls("chat.postMessage"))
                await asyncio.sleep(0.2)
                return rig.slack
        assert len(run(scenario()).calls("chat.postMessage")) == 1

    def test_a_secret_in_the_agents_answer_never_reaches_the_wire(self):
        async def scenario():
            async with Rig(with_runner=True, agent_factory=lambda *a: FakeAgent(reply=f"key: {BOT_TOKEN}")) as rig:
                await rig.slack.push_event(dm(), "env-1")
                await wait_for(lambda: rig.slack.calls("chat.postMessage"))
                return rig.slack
        text = run(scenario()).calls("chat.postMessage")[0]["text"]
        assert BOT_TOKEN not in text and "[redacted]" in text


@pytest.mark.parametrize("attr", ["_socket", "_web"])
def test_disconnect_is_idempotent(attr):
    async def scenario():
        async with Rig() as rig:
            await rig.adapter.disconnect()
            await rig.adapter.disconnect()
    run(scenario())
