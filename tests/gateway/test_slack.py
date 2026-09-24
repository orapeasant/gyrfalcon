"""The Slack adapter, against a faked Web API and socket — no network.

What can only be checked against a real workspace (scopes, Socket Mode
enablement, real event payload quirks) is listed in docs/slack/README.md."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from _fakes import FakeAgent, FakeSessionDB, run, wait_for

# The Slack SDK is an optional extra (`uv sync --extra slack`), so these skip
# rather than break collection for an install that does not use Slack — the same
# courtesy the PostgreSQL tests extend. Without this, a plain `uv run pytest`
# (which prunes extras) fails the entire suite at import.
pytest.importorskip("slack_sdk", reason="install with: uv sync --extra slack")

from slack_sdk.errors import SlackApiError  # noqa: E402
from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: E402

from gyrfalcon.gateway.config import GatewayConfig, PlatformConfig  # noqa: E402
from gyrfalcon.gateway.platforms.base import (  # noqa: E402
    DONE,
    FAILED,
    QUEUED,
    WORKING,
    MessageEvent,
    SessionSource,
)
from gyrfalcon.gateway.platforms.slack import SlackAdapter  # noqa: E402
from gyrfalcon.gateway.run import GatewayRunner  # noqa: E402

BOT = "UBOT0001"
BOT_TOKEN = "xoxb-fixture-only"
APP_TOKEN = "xapp-1-A0123-3333333333-0123456789abcdef"


class FakeWeb:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.retry_handlers: list = []
        self.fail: dict[str, Exception] = {}
        self.channel_name = "ops"
        self._n = 0

    def _record(self, name, kw):
        self.calls.append((name, kw))
        if name in self.fail:
            raise self.fail[name]

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]

    async def auth_test(self, **kw):
        self._record("auth_test", kw)
        return {"user_id": BOT, "team_id": "T1", "user": "gyrfalcon"}

    async def chat_postMessage(self, **kw):
        self._record("chat_postMessage", kw)
        self._n += 1
        return {"ts": f"9000.{self._n:04d}"}

    async def chat_update(self, **kw):
        self._record("chat_update", kw)
        return {}

    async def reactions_add(self, **kw):
        self._record("reactions_add", kw)
        return {}

    async def reactions_remove(self, **kw):
        self._record("reactions_remove", kw)
        return {}

    async def conversations_info(self, **kw):
        self._record("conversations_info", kw)
        return {"channel": {"name": self.channel_name, "is_im": False, "is_private": False, "num_members": 4}}


class FakeSocket:
    def __init__(self, fail_connect: Exception | None = None):
        self.socket_mode_request_listeners: list = []
        self.acks: list[str] = []
        self.connected = False
        self.closed = False
        self.fail_connect = fail_connect
        self.ack_error: Exception | None = None

    async def connect(self):
        if self.fail_connect:
            raise self.fail_connect
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def close(self):
        self.closed = True

    async def send_socket_mode_response(self, response):
        if self.ack_error:
            raise self.ack_error
        self.acks.append(response.envelope_id)


def api_error(code: str) -> SlackApiError:
    return SlackApiError(f"The request to the Slack API failed. (error: {code})", {"ok": False, "error": code})


def cfg(**extra) -> PlatformConfig:
    raw = {"allow": {"users": ["U1"], "channels": ["C1"]}, **extra}
    return PlatformConfig.from_config("slack", raw)


def make(config: PlatformConfig | None = None, web: FakeWeb | None = None, socket: FakeSocket | None = None):
    web, socket = web or FakeWeb(), socket or FakeSocket()
    adapter = SlackAdapter(config or cfg(), web_client=web, socket_factory=lambda tok, w: socket)
    return adapter, web, socket


def connected(config=None, **kw):
    adapter, web, socket = make(config, **kw)
    adapter._bot_user_id, adapter._team_id = BOT, "T1"
    return adapter, web, socket


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("SLACK_APP_TOKEN", APP_TOKEN)


def payload(**event) -> dict:
    base = {"type": "message", "user": "U1", "channel": "D1", "channel_type": "im", "ts": "1000.0001", "text": "hello"}
    return {"team_id": "T1", "event_id": "Ev1", "event": {**base, **event}}


def request(pl: dict, type="events_api", envelope="env-1") -> SocketModeRequest:
    return SocketModeRequest(type=type, envelope_id=envelope, payload=pl)


async def to_event(adapter, **event):
    pl = payload(**event)
    return await adapter._to_event(pl["event"], pl)


# ── connecting ────────────────────────────────────────────────────────────────

class TestConnect:
    def test_success_wires_the_listener_and_learns_who_we_are(self):
        async def scenario():
            adapter, web, socket = make()
            ok = await adapter.connect()
            return adapter, web, socket, ok
        adapter, web, socket, ok = run(scenario())
        assert ok and socket.connected
        assert adapter._on_request in socket.socket_mode_request_listeners
        assert adapter._bot_user_id == BOT and adapter._team_id == "T1"

    def test_the_adapter_reports_its_tokens_so_the_runner_can_scrub_them(self):
        async def scenario():
            adapter, _, _ = make()
            await adapter.connect()
            return adapter
        assert set(run(scenario()).secrets()) == {BOT_TOKEN, APP_TOKEN}

    @pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
    def test_a_missing_token_fails_cleanly_and_touches_nothing(self, monkeypatch, missing, caplog):
        monkeypatch.delenv(missing)
        adapter, web, socket = make()
        with caplog.at_level(logging.ERROR):
            assert run(adapter.connect()) is False
        assert web.calls == [] and not socket.connected
        assert "bot token" in caplog.text or "app-level token" in caplog.text

    @pytest.mark.parametrize("var,value", [("SLACK_BOT_TOKEN", "xapp-wrong-kind-of-token"),
                                           ("SLACK_APP_TOKEN", "xoxb-fixture-only")])
    def test_swapped_token_kinds_are_caught_before_any_request(self, monkeypatch, var, value):
        monkeypatch.setenv(var, value)
        adapter, web, _ = make()
        assert run(adapter.connect()) is False
        assert web.calls == []

    def test_a_rejected_bot_token_is_reported_without_echoing_it(self, caplog):
        web = FakeWeb()
        web.fail["auth_test"] = api_error("invalid_auth")
        adapter, _, socket = make(web=web)
        with caplog.at_level(logging.ERROR):
            assert run(adapter.connect()) is False
        assert "invalid_auth" in caplog.text
        assert BOT_TOKEN not in caplog.text and not socket.connected

    def test_a_socket_failure_is_reported_and_names_the_likely_cause(self, caplog):
        adapter, _, _ = make(socket=FakeSocket(fail_connect=RuntimeError("boom")))
        with caplog.at_level(logging.ERROR):
            assert run(adapter.connect()) is False
        assert "connections:write" in caplog.text

    def test_only_socket_mode_is_implemented(self, caplog):
        adapter, web, _ = make(cfg(mode="events"))
        with caplog.at_level(logging.ERROR):
            assert run(adapter.connect()) is False
        assert web.calls == [] and "only mode 'socket'" in caplog.text

    def test_tokens_in_config_yaml_are_ignored_and_warned_about(self, monkeypatch, caplog):
        # The dashboard's config API returns that file: a token there is served over HTTP.
        monkeypatch.delenv("SLACK_BOT_TOKEN")
        adapter, web, _ = make(cfg(bot_token=BOT_TOKEN))
        with caplog.at_level(logging.WARNING):
            assert run(adapter.connect()) is False
        assert "Ignoring 'bot_token' in config.yaml" in caplog.text
        assert BOT_TOKEN not in caplog.text

    def test_tokens_come_from_the_secret_store_by_name(self, tmp_path, monkeypatch):
        from gyrfalcon import security
        from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

        monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
        monkeypatch.delenv("SLACK_BOT_TOKEN")
        monkeypatch.delenv("SLACK_APP_TOKEN")
        get_gyrfalcon_home.cache_clear()
        try:
            security.create_secret("my_bot", "bot", BOT_TOKEN)
            security.create_secret("my_app", "app", APP_TOKEN)
            adapter, _, socket = make(cfg(bot_token_secret="my_bot", app_token_secret="my_app"))
            assert run(adapter.connect()) is True and socket.connected
        finally:
            get_gyrfalcon_home.cache_clear()

    def test_the_secret_store_wins_over_the_environment(self, tmp_path, monkeypatch):
        from gyrfalcon import security
        from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

        monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
        get_gyrfalcon_home.cache_clear()
        try:
            security.create_secret("my_bot", "bot", "xoxb-fixture-only")
            adapter, _, _ = make(cfg(bot_token_secret="my_bot"))
            assert adapter._resolve_token("bot") == "xoxb-fixture-only"
        finally:
            get_gyrfalcon_home.cache_clear()

    def test_a_real_web_client_is_built_with_rate_limit_retries(self, monkeypatch):
        import gyrfalcon.gateway.platforms.slack as slack_mod
        built = []

        class Web(FakeWeb):
            def __init__(self, token):
                super().__init__()
                built.append(token)

        monkeypatch.setattr(slack_mod, "AsyncWebClient", Web)
        socket = FakeSocket()
        adapter = SlackAdapter(cfg(), socket_factory=lambda t, w: socket)
        assert run(adapter.connect()) is True
        assert built == [BOT_TOKEN] and len(adapter._web.retry_handlers) == 1

    def test_the_default_socket_client_is_a_real_one(self):
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.web.async_client import AsyncWebClient

        async def scenario():
            client = SlackAdapter._default_socket(APP_TOKEN, AsyncWebClient(token=BOT_TOKEN))
            assert isinstance(client, SocketModeClient) and client.auto_reconnect_enabled
            await client.close()
        run(scenario())

    def test_disconnect_closes_the_socket_and_cancels_in_flight_work(self):
        async def scenario():
            adapter, _, socket = make()
            await adapter.connect()
            gate = asyncio.Event()

            async def slow(_):
                await gate.wait()
            adapter.on_message(slow)
            await asyncio.wait_for(adapter._on_request(socket, request(payload())), 1)
            await wait_for(lambda: len(adapter._tasks) == 1)
            await adapter.disconnect()
            return adapter, socket
        adapter, socket = run(scenario())
        assert socket.closed and not adapter._tasks


class TestApiErrorFormatting:
    """`_api_error` runs inside `except` blocks: if it raises, a failed send becomes
    a crash and an inbound message is silently dropped. Found by running the real
    SDK against a fake server — on a non-200 the response body is a string."""

    def _fmt(self, exc):
        from gyrfalcon.gateway.platforms.slack import _api_error
        return _api_error(exc)

    def test_a_normal_slack_error_code(self):
        assert self._fmt(api_error("channel_not_found")) == "channel_not_found"

    def test_a_string_body_from_a_non_json_reply_does_not_raise(self):
        class Resp:
            data = "<html>502 Bad Gateway</html>"
            status_code = 502

            def get(self, key, default=None):
                return self.data.get(key, default)  # AttributeError, as in the real SDK
        assert self._fmt(SlackApiError("failed", Resp())) == "http_502"

    def test_a_response_whose_get_explodes_is_survived(self):
        class Resp:
            def get(self, *a):
                raise AttributeError("'str' object has no attribute 'get'")
        assert self._fmt(SlackApiError("failed", Resp())) == "slack_api_error"

    def test_a_missing_error_field_falls_back_to_the_http_status(self):
        class Resp:
            data = {"ok": False}
            status_code = 429
        assert self._fmt(SlackApiError("slow down", Resp())) == "http_429"

    def test_nothing_at_all_still_gives_an_answer(self):
        assert self._fmt(SlackApiError("failed", None)) == "slack_api_error"
        assert self._fmt(SlackApiError("failed", "just a string")) == "slack_api_error"

    def test_other_exceptions_are_named_by_type_only(self):
        assert self._fmt(ConnectionResetError(f"reset while sending {BOT_TOKEN}")) == "ConnectionResetError"

    def test_the_message_text_never_appears(self):
        class Resp:
            data = {"error": "invalid_auth"}
        out = self._fmt(SlackApiError(f"failed with token {BOT_TOKEN}", Resp()))
        assert out == "invalid_auth" and BOT_TOKEN not in out

    def test_a_failed_send_on_a_non_json_reply_returns_a_result_instead_of_raising(self):
        class Resp:
            data = "Bad Gateway"
            status_code = 502

            def get(self, key, default=None):
                return self.data.get(key, default)

        async def scenario():
            web = FakeWeb()
            web.fail["chat_postMessage"] = SlackApiError("failed", Resp())
            adapter, _, _ = connected(web=web)
            return await adapter.send("C1", "hi")
        r = run(scenario())
        assert r.success is False and r.error == "http_502"

    def test_an_inbound_message_survives_a_non_json_failure_while_looking_up_the_channel_name(self):
        class Resp:
            data = "Bad Gateway"
            status_code = 502

            def get(self, key, default=None):
                return self.data.get(key, default)

        async def scenario():
            web = FakeWeb()
            web.fail["conversations_info"] = SlackApiError("failed", Resp())
            adapter, _, _ = connected(web=web)
            return await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                                  text=f"<@{BOT}> still delivered?")
        e = run(scenario())
        assert e is not None and e.text == "still delivered?" and e.source.chat_name == ""


# ── what becomes a message for the agent ──────────────────────────────────────

class TestInboundFiltering:
    def test_a_dm_becomes_one_rolling_conversation(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter)
        e = run(scenario())
        assert e.text == "hello" and e.event_id == "D1:1000.0001"
        s = e.source
        assert (s.platform, s.chat_id, s.user_id, s.chat_type) == ("slack", "D1", "U1", "dm")
        assert s.thread_id == "" and s.message_id == "1000.0001" and s.guild_id == "T1"
        assert s.session_key == "slack:D1:"

    def test_two_dm_messages_share_a_session(self):
        async def scenario():
            adapter, _, _ = connected()
            a = await to_event(adapter, ts="1.1")
            b = await to_event(adapter, ts="2.2")
            return a, b
        a, b = run(scenario())
        assert a.source.session_key == b.source.session_key

    def test_a_dm_thread_is_its_own_conversation(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, thread_ts="500.5", ts="501.1")
        e = run(scenario())
        assert e.source.thread_id == "500.5" and e.source.session_key == "slack:D1:500.5"

    def test_a_channel_mention_opens_a_thread_on_the_message_and_strips_the_mention(self):
        async def scenario():
            adapter, web, _ = connected()
            e = await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                               ts="700.1", text=f"<@{BOT}> what changed?")
            return e, web
        e, web = run(scenario())
        assert e.text == "what changed?"
        assert e.source.thread_id == "700.1" and e.source.chat_type == "channel"
        assert e.source.chat_name == "ops", "resolved so routing rules like ^#ops can match"
        assert len(web.of("conversations_info")) == 1

    def test_a_mention_inside_a_thread_joins_that_thread(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                                  ts="702.2", thread_ts="700.1", text=f"<@{BOT}> and now?")
        assert run(scenario()).source.thread_id == "700.1"

    def test_channel_names_are_cached(self):
        async def scenario():
            adapter, web, _ = connected()
            for ts in ("1.1", "2.2", "3.3"):
                await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                               text=f"<@{BOT}> x", ts=ts)
            return web
        assert len(run(scenario()).of("conversations_info")) == 1

    def test_a_missing_channels_read_scope_degrades_to_id_routing(self, caplog):
        async def scenario():
            web = FakeWeb()
            web.fail["conversations_info"] = api_error("missing_scope")
            adapter, _, _ = connected(web=web)
            with caplog.at_level(logging.WARNING):
                return await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                                      text=f"<@{BOT}> x")
        e = run(scenario())
        assert e is not None and e.source.chat_name == ""
        assert "channels:read" in caplog.text

    def test_plain_channel_messages_are_ignored_unless_we_are_in_the_thread(self):
        async def scenario():
            adapter, _, _ = connected()
            top_level = await to_event(adapter, channel="C1", channel_type="channel", text="unrelated chatter")
            stranger_thread = await to_event(adapter, channel="C1", channel_type="channel",
                                             thread_ts="1.1", ts="2.2", text="replying to someone else")
            return top_level, stranger_thread
        assert run(scenario()) == (None, None)

    def test_a_follow_up_in_a_thread_we_joined_needs_no_mention(self):
        async def scenario():
            adapter, _, _ = connected()
            await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                           ts="700.1", text=f"<@{BOT}> start")
            return await to_event(adapter, channel="C1", channel_type="channel",
                                  thread_ts="700.1", ts="701.1", text="and another thing")
        e = run(scenario())
        assert e is not None and e.text == "and another thing" and e.source.thread_id == "700.1"

    def test_a_thread_we_replied_in_counts_as_joined(self):
        async def scenario():
            adapter, _, _ = connected()
            await adapter.send("C1", "hi", reply_to="800.1")
            return await to_event(adapter, channel="C1", channel_type="channel", thread_ts="800.1", ts="801.1")
        assert run(scenario()) is not None

    def test_a_plain_message_that_mentions_us_is_left_to_its_app_mention_twin(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, channel="C1", channel_type="channel", thread_ts="1.1",
                                  text=f"<@{BOT}> hi")
        assert run(scenario()) is None

    def test_slack_delivers_a_mention_as_two_events_and_only_one_runs(self):
        async def scenario():
            adapter, _, socket = connected()
            seen = []

            async def collect(e):
                seen.append(e)
            adapter.on_message(collect)
            text = f"<@{BOT}> deploy status?"
            common = dict(channel="C1", channel_type="channel", ts="900.1", text=text)
            await adapter._on_request(socket, request(payload(type="message", **common), envelope="e1"))
            await adapter._on_request(socket, request(payload(type="app_mention", **common), envelope="e2"))
            await wait_for(lambda: not adapter._tasks)
            return seen
        assert len(run(scenario())) == 1

    def test_a_mention_inside_a_thread_we_own_is_still_only_one_run(self):
        # The stronger case: here the plain `message` twin would otherwise pass the
        # "thread we joined" rule too, so only the mention check stops the double.
        async def scenario():
            adapter, _, socket = connected()
            await adapter.send("C1", "earlier reply", reply_to="700.1")
            seen = []

            async def collect(e):
                seen.append(e)
            adapter.on_message(collect)
            common = dict(channel="C1", channel_type="channel", ts="710.1", thread_ts="700.1",
                          text=f"<@{BOT}> and this?")
            await adapter._on_request(socket, request(payload(type="message", **common), envelope="e1"))
            await adapter._on_request(socket, request(payload(type="app_mention", **common), envelope="e2"))
            await wait_for(lambda: not adapter._tasks)
            return seen
        seen = run(scenario())
        assert len(seen) == 1 and seen[0].text == "and this?"

    def test_we_never_answer_ourselves(self):
        async def scenario():
            adapter, _, _ = connected()
            by_id = await to_event(adapter, user=BOT)
            by_bot_id = await to_event(adapter, bot_id="B123")
            return by_id, by_bot_id
        assert run(scenario()) == (None, None)

    @pytest.mark.parametrize("subtype", ["message_changed", "message_deleted", "bot_message", "channel_join",
                                         "channel_topic", "me_message"])
    def test_noise_subtypes_are_dropped(self, subtype):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, subtype=subtype)
        assert run(scenario()) is None

    def test_a_shared_file_is_acknowledged_honestly_rather_than_ignored(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, subtype="file_share", text="see attached",
                                  files=[{"id": "F1"}, {"id": "F2"}])
        e = run(scenario())
        assert e.text.startswith("see attached") and "2 attachment(s)" in e.text and "cannot read" in e.text

    def test_thread_broadcasts_are_handled_like_replies(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, subtype="thread_broadcast", thread_ts="5.5", ts="6.6")
        assert run(scenario()) is not None

    @pytest.mark.parametrize("missing", ["user", "channel", "ts"])
    def test_incomplete_events_are_dropped(self, missing):
        async def scenario():
            adapter, _, _ = connected()
            pl = payload()
            del pl["event"][missing]
            return await adapter._to_event(pl["event"], pl)
        assert run(scenario()) is None

    def test_other_event_types_are_dropped(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, type="reaction_added")
        assert run(scenario()) is None

    @pytest.mark.parametrize("cid,expected", [("D9", "dm"), ("C9", "channel"), ("G9", "channel")])
    def test_the_chat_type_is_inferred_when_slack_omits_it(self, cid, expected):
        async def scenario():
            adapter, _, _ = connected()
            pl = payload(type="app_mention", channel=cid, text=f"<@{BOT}> hi")
            del pl["event"]["channel_type"]
            return await adapter._to_event(pl["event"], pl)
        assert run(scenario()).source.chat_type == expected

    def test_a_group_dm_is_a_shared_space(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, type="app_mention", channel="G1", channel_type="mpim", text=f"<@{BOT}> hi")
        assert run(scenario()).source.chat_type == "group"

    def test_slack_markup_is_made_readable_for_the_model(self):
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, text="see <https://x.io|the docs> &amp; <#C1|ops>")
        assert run(scenario()).text == "see the docs (https://x.io) & #ops"

    @pytest.mark.parametrize("key,event", [
        ("dms", dict()),
        ("mentions", dict(type="app_mention", channel="C1", channel_type="channel", text=f"<@{BOT}> hi")),
    ])
    def test_respond_to_switches(self, key, event):
        async def scenario():
            adapter, _, _ = connected(cfg(respond_to={key: False}))
            return await to_event(adapter, **event)
        assert run(scenario()) is None

    def test_respond_to_threads_off_requires_a_mention_every_time(self):
        async def scenario():
            adapter, _, _ = connected(cfg(respond_to={"threads": False}))
            await to_event(adapter, type="app_mention", channel="C1", channel_type="channel",
                           ts="700.1", text=f"<@{BOT}> start")
            return await to_event(adapter, channel="C1", channel_type="channel", thread_ts="700.1", ts="701.1")
        assert run(scenario()) is None

    def test_the_adapter_does_not_authorise_that_is_the_runners_job(self):
        # A stranger's message is still translated and handed over; the runner's
        # allowlist refuses it. One decision point, so no adapter can forget it.
        async def scenario():
            adapter, _, _ = connected()
            return await to_event(adapter, user="USTRANGER")
        assert run(scenario()).source.user_id == "USTRANGER"


# ── the socket listener ───────────────────────────────────────────────────────

class TestListener:
    def test_it_acknowledges_before_doing_anything_else(self):
        async def scenario():
            adapter, _, socket = connected()
            order = []

            async def handler(e):
                order.append(("dispatch", list(socket.acks)))
            adapter.on_message(handler)
            await adapter._on_request(socket, request(payload(), envelope="env-42"))
            await wait_for(lambda: order)
            return order, socket
        order, socket = run(scenario())
        assert socket.acks == ["env-42"]
        assert order == [("dispatch", ["env-42"])], "the ack was already sent when the event was handled"

    def test_it_returns_immediately_even_if_the_turn_takes_minutes(self):
        # Awaiting the agent here would stall the socket's reads: Slack would
        # redeliver every event and eventually drop the connection.
        async def scenario():
            adapter, _, socket = connected()
            gate = asyncio.Event()
            started = asyncio.Event()

            async def slow(e):
                started.set()
                await gate.wait()
            adapter.on_message(slow)
            await asyncio.wait_for(adapter._on_request(socket, request(payload())), 0.5)
            await asyncio.wait_for(started.wait(), 1)
            in_flight = len(adapter._tasks)
            gate.set()
            await wait_for(lambda: not adapter._tasks)
            return in_flight
        assert run(scenario()) == 1

    @pytest.mark.parametrize("kind", ["slash_commands", "interactive", "hello"])
    def test_other_envelope_types_are_acknowledged_but_not_dispatched(self, kind):
        async def scenario():
            adapter, _, socket = connected()
            seen = []

            async def collect(e):
                seen.append(e)
            adapter.on_message(collect)
            await adapter._on_request(socket, request(payload(), type=kind, envelope="e9"))
            await asyncio.sleep(0.02)
            return seen, socket
        seen, socket = run(scenario())
        assert socket.acks == ["e9"] and seen == []

    def test_a_failed_ack_does_not_stop_the_event_being_handled(self):
        async def scenario():
            adapter, _, socket = connected()
            socket.ack_error = ConnectionError("socket closed")
            seen = []

            async def collect(e):
                seen.append(e)
            adapter.on_message(collect)
            await adapter._on_request(socket, request(payload()))
            await wait_for(lambda: seen)
            return seen
        assert len(run(scenario())) == 1

    def test_an_error_while_handling_is_logged_not_raised(self, caplog):
        async def scenario():
            adapter, _, socket = connected()

            async def boom(e):
                raise RuntimeError("handler exploded")
            adapter.on_message(boom)
            with caplog.at_level(logging.ERROR):
                await adapter._on_request(socket, request(payload()))
                await wait_for(lambda: not adapter._tasks)
            return adapter
        run(scenario())
        assert "Slack: error handling event" in caplog.text

    def test_a_malformed_payload_is_survived(self):
        async def scenario():
            adapter, _, socket = connected()
            adapter.on_message(lambda e: asyncio.sleep(0))
            await adapter._on_request(socket, request({"event": "not-a-dict"}))
            await adapter._on_request(socket, request({}))
            await asyncio.sleep(0.02)
            return socket
        assert run(scenario()).acks == ["env-1", "env-1"]


# ── sending ───────────────────────────────────────────────────────────────────

def dm_source(thread_id="") -> SessionSource:
    return SessionSource(platform="slack", chat_id="D1", user_id="U1", chat_type="dm", thread_id=thread_id,
                         message_id="1000.0001")


def channel_source(thread_id="700.1") -> SessionSource:
    return SessionSource(platform="slack", chat_id="C1", user_id="U1", chat_type="channel", thread_id=thread_id,
                         message_id="701.1")


class TestSend:
    def test_it_posts_converted_text_with_the_safe_options(self):
        async def scenario():
            adapter, web, _ = connected()
            r = await adapter.send("C1", "**bold** [docs](https://x.io)", reply_to="700.1",
                                   metadata={"source": channel_source()})
            return r, web
        r, web = run(scenario())
        (post,) = web.of("chat_postMessage")
        assert post["channel"] == "C1" and post["thread_ts"] == "700.1"
        assert post["text"] == "*bold* <https://x.io|docs>"
        assert (post["mrkdwn"], post["parse"], post["link_names"]) == (True, "none", False)
        assert post["unfurl_links"] is False and post["unfurl_media"] is False
        assert r.success and r.message_id == "9000.0001"

    def test_an_at_channel_in_model_output_cannot_page_the_workspace(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.send("C1", "<!channel> <!here> <@U0999> hi", reply_to="1.1")
            return web
        text = run(scenario()).of("chat_postMessage")[0]["text"]
        assert "<!" not in text and "<@" not in text and "&lt;!channel&gt;" in text

    def test_a_top_level_dm_reply_is_not_threaded(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.send("D1", "hi", reply_to="1000.0001", metadata={"source": dm_source()})
            return web
        assert run(scenario()).of("chat_postMessage")[0]["thread_ts"] is None

    def test_a_reply_inside_a_dm_thread_stays_in_it(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.send("D1", "hi", reply_to="500.5", metadata={"source": dm_source("500.5")})
            return web
        assert run(scenario()).of("chat_postMessage")[0]["thread_ts"] == "500.5"

    def test_long_replies_are_split_and_every_piece_is_posted_in_order(self):
        async def scenario():
            adapter, web, _ = connected()
            body = "\n\n".join(f"paragraph {i} " + "word " * 120 for i in range(20))
            r = await adapter.send("C1", body, reply_to="1.1")
            return r, web
        r, web = run(scenario())
        posts = web.of("chat_postMessage")
        assert len(posts) > 2 and all(len(p["text"]) <= 3800 + 4 for p in posts)
        assert all(p["thread_ts"] == "1.1" for p in posts)
        assert r.message_id == "9000.0001", "the first message's ts, which is the one a later edit would target"

    def test_a_slack_error_becomes_a_token_free_failure(self):
        async def scenario():
            web = FakeWeb()
            web.fail["chat_postMessage"] = api_error("channel_not_found")
            adapter, _, _ = connected(web=web)
            return await adapter.send("C9", "hi", reply_to="1.1")
        r = run(scenario())
        assert r.success is False and r.error == "channel_not_found"

    def test_a_non_slack_exception_is_reported_by_type_only(self):
        async def scenario():
            web = FakeWeb()
            web.fail["chat_postMessage"] = ConnectionError(f"failed talking to slack with {BOT_TOKEN}")
            adapter, _, _ = connected(web=web)
            return await adapter.send("C1", "hi")
        r = run(scenario())
        assert r.error == "ConnectionError" and BOT_TOKEN not in (r.error or "")


class TestEditAndInfo:
    def test_edit_updates_in_place_with_converted_text(self):
        async def scenario():
            adapter, web, _ = connected()
            ok = await adapter.edit_message("C1", "9000.0001", "**partial** answer")
            return ok, web
        ok, web = run(scenario())
        assert ok and web.of("chat_update") == [
            {"channel": "C1", "ts": "9000.0001", "text": "*partial* answer", "parse": "none", "link_names": False}]

    def test_an_over_long_edit_is_truncated_not_rejected(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.edit_message("C1", "1.1", "x" * 10000)
            return web
        text = run(scenario()).of("chat_update")[0]["text"]
        assert len(text) <= 3800 and text.endswith("…")

    def test_a_failed_edit_is_false_not_an_exception(self):
        async def scenario():
            web = FakeWeb()
            web.fail["chat_update"] = api_error("message_not_found")
            adapter, _, _ = connected(web=web)
            return await adapter.edit_message("C1", "1.1", "x")
        assert run(scenario()) is False

    def test_chat_info(self):
        async def scenario():
            adapter, _, _ = connected()
            return await adapter.get_chat_info("C1")
        assert run(scenario()) == {"id": "C1", "name": "ops", "is_im": False, "is_private": False, "num_members": 4}

    def test_chat_info_failure_is_data_not_a_crash(self):
        async def scenario():
            web = FakeWeb()
            web.fail["conversations_info"] = api_error("channel_not_found")
            adapter, _, _ = connected(web=web)
            return await adapter.get_chat_info("C9")
        assert run(scenario()) == {"id": "C9", "error": "channel_not_found"}


# ── progress reactions ────────────────────────────────────────────────────────

def message_event(ts="1000.0001", text="hi") -> MessageEvent:
    return MessageEvent(source=dm_source() if False else SessionSource(
        platform="slack", chat_id="D1", user_id="U1", chat_type="dm", message_id=ts), text=text)


class TestReactions:
    def _steps(self, web):
        return [(n, kw["name"]) for n, kw in web.calls if n.startswith("reactions_")]

    def test_a_turn_walks_through_working_then_done(self):
        async def scenario():
            adapter, web, _ = connected()
            e = message_event()
            await adapter.acknowledge(e, WORKING)
            await adapter.acknowledge(e, DONE)
            return web
        assert self._steps(run(scenario())) == [
            ("reactions_add", "hourglass_flowing_sand"),
            ("reactions_remove", "hourglass_flowing_sand"), ("reactions_add", "white_check_mark")]

    def test_a_queued_message_shows_eyes_then_swaps_when_its_turn_is_done(self):
        async def scenario():
            adapter, web, _ = connected()
            e = message_event()
            await adapter.acknowledge(e, QUEUED)
            await adapter.acknowledge(e, DONE)
            return web
        assert self._steps(run(scenario())) == [
            ("reactions_add", "eyes"), ("reactions_remove", "eyes"), ("reactions_add", "white_check_mark")]

    def test_failure_shows_a_cross(self):
        async def scenario():
            adapter, web, _ = connected()
            e = message_event()
            await adapter.acknowledge(e, WORKING)
            await adapter.acknowledge(e, FAILED)
            return web
        assert self._steps(run(scenario()))[-1] == ("reactions_add", "x")

    def test_repeating_a_state_does_not_repeat_the_call(self):
        async def scenario():
            adapter, web, _ = connected()
            e = message_event()
            await adapter.acknowledge(e, WORKING)
            await adapter.acknowledge(e, WORKING)
            return web
        assert len(self._steps(run(scenario()))) == 1

    def test_reactions_target_the_users_message(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.acknowledge(message_event(ts="42.42"), DONE)
            return web
        kw = run(scenario()).of("reactions_add")[0]
        assert (kw["channel"], kw["timestamp"]) == ("D1", "42.42")

    def test_benign_errors_are_silent(self, caplog):
        async def scenario():
            web = FakeWeb()
            web.fail["reactions_add"] = api_error("already_reacted")
            adapter, _, _ = connected(web=web)
            with caplog.at_level(logging.WARNING):
                await adapter.acknowledge(message_event(), DONE)
        run(scenario())
        assert "reaction failed" not in caplog.text

    def test_a_missing_scope_warns_once_with_the_fix(self, caplog):
        async def scenario():
            web = FakeWeb()
            web.fail["reactions_add"] = api_error("missing_scope")
            adapter, _, _ = connected(web=web)
            with caplog.at_level(logging.WARNING):
                await adapter.acknowledge(message_event(ts="1.1"), WORKING)
                await adapter.acknowledge(message_event(ts="2.2"), WORKING)
        run(scenario())
        assert caplog.text.count("reactions:write") == 1

    def test_no_message_id_means_nothing_to_react_to(self):
        async def scenario():
            adapter, web, _ = connected()
            bare = MessageEvent(source=SessionSource(platform="slack", chat_id="D1"), text="x")
            await adapter.acknowledge(bare, DONE)
            return web
        assert run(scenario()).calls == []

    def test_unknown_states_are_ignored(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.acknowledge(message_event(), "bogus")
            return web
        assert run(scenario()).calls == []


# ── the whole path, with the real runner ──────────────────────────────────────

class TestEndToEnd:
    """Slack events in, a real GatewayRunner in the middle, Slack API calls out."""

    def _rig(self, agent_factory=None, platform=None):
        adapter, web, socket = connected()
        config = GatewayConfig.from_dict({"platforms": {"slack": platform or {
            "allow": {"users": ["U1"], "channels": ["C1"]}}}})
        adapter.config = config.platforms["slack"]
        runner = GatewayRunner(
            config, agent_factory=agent_factory or (lambda src, sid, st: FakeAgent(reply="**done** ✔")),
            session_db=FakeSessionDB(), run_scheduler=False,
            adapter_loader=lambda name: (lambda c: adapter),
        )
        runner._create_adapter("slack")  # wires adapter -> runner.handle_event
        return runner, adapter, web, socket

    def test_a_dm_gets_a_formatted_unthreaded_reply_and_progress_reactions(self):
        async def scenario():
            runner, adapter, web, socket = self._rig()
            await adapter._on_request(socket, request(payload(text="hello there")))
            await wait_for(lambda: web.of("chat_postMessage") and web.of("reactions_add")
                           and web.of("reactions_add")[-1]["name"] == "white_check_mark")
            await wait_for(lambda: not adapter._tasks)
            return web
        web = run(scenario())
        (post,) = web.of("chat_postMessage")
        assert post["text"] == "*done* ✔" and post["thread_ts"] is None and post["channel"] == "D1"
        assert [kw["name"] for kw in web.of("reactions_add")] == ["hourglass_flowing_sand", "white_check_mark"]

    def test_a_stranger_gets_nothing_and_no_agent_is_built(self):
        built = []

        async def scenario():
            runner, adapter, web, socket = self._rig(agent_factory=lambda *a: built.append(a) or FakeAgent())
            await adapter._on_request(socket, request(payload(user="USTRANGER")))
            await wait_for(lambda: not adapter._tasks)
            return web
        web = run(scenario())
        assert built == [] and web.of("chat_postMessage") == [] and web.of("reactions_add") == []

    def test_a_channel_mention_is_answered_in_a_thread_on_the_message(self):
        async def scenario():
            runner, adapter, web, socket = self._rig()
            await adapter._on_request(socket, request(payload(
                type="app_mention", channel="C1", channel_type="channel", ts="700.1", text=f"<@{BOT}> status?")))
            await wait_for(lambda: web.of("chat_postMessage"))
            await wait_for(lambda: not adapter._tasks)
            return web
        (post,) = run(scenario()).of("chat_postMessage")
        assert post["channel"] == "C1" and post["thread_ts"] == "700.1"

    def test_a_channel_the_user_is_not_allowed_in_gets_nothing(self):
        async def scenario():
            runner, adapter, web, socket = self._rig()
            await adapter._on_request(socket, request(payload(
                type="app_mention", channel="C9", channel_type="channel", text=f"<@{BOT}> status?")))
            await wait_for(lambda: not adapter._tasks)
            return web
        assert run(scenario()).of("chat_postMessage") == []

    def test_the_follow_up_in_the_thread_needs_no_mention_and_reuses_the_conversation(self):
        agents = []

        async def scenario():
            runner, adapter, web, socket = self._rig(agent_factory=lambda *a: agents.append(FakeAgent()) or agents[-1])
            await adapter._on_request(socket, request(payload(
                type="app_mention", channel="C1", channel_type="channel", ts="700.1",
                text=f"<@{BOT}> first"), envelope="a"))
            await wait_for(lambda: len(web.of("chat_postMessage")) == 1)
            await adapter._on_request(socket, request(payload(
                channel="C1", channel_type="channel", ts="701.1", thread_ts="700.1", text="second"), envelope="b"))
            await wait_for(lambda: len(web.of("chat_postMessage")) == 2)
            return web
        run(scenario())
        assert len(agents) == 1 and [c["message"] for c in agents[0].calls] == ["first", "second"]

    def test_the_bots_own_reply_arriving_as_an_event_does_not_loop(self):
        async def scenario():
            runner, adapter, web, socket = self._rig()
            await adapter._on_request(socket, request(payload(text="hi")))
            await wait_for(lambda: web.of("chat_postMessage"))
            # Slack echoes our reply back to us as a message event.
            await adapter._on_request(socket, request(payload(user=BOT, bot_id="B1", text="*done*", ts="9000.0001"),
                                                      envelope="echo"))
            await wait_for(lambda: not adapter._tasks)
            return web
        assert len(run(scenario()).of("chat_postMessage")) == 1

    def test_a_token_in_the_agents_answer_never_reaches_slack(self):
        async def scenario():
            runner, adapter, web, socket = self._rig(
                agent_factory=lambda *a: FakeAgent(reply=f"the token is {BOT_TOKEN} and {APP_TOKEN}"))
            await adapter._on_request(socket, request(payload()))
            await wait_for(lambda: web.of("chat_postMessage"))
            await wait_for(lambda: not adapter._tasks)
            return web
        text = run(scenario()).of("chat_postMessage")[0]["text"]
        assert BOT_TOKEN not in text and APP_TOKEN not in text and "[redacted]" in text

    def test_slack_redelivering_an_event_runs_it_once(self):
        agents = []

        async def scenario():
            runner, adapter, web, socket = self._rig(agent_factory=lambda *a: agents.append(FakeAgent()) or agents[-1])
            for env in ("first", "retry"):
                await adapter._on_request(socket, request(payload(), envelope=env))
            await wait_for(lambda: web.of("chat_postMessage"))
            await wait_for(lambda: not adapter._tasks)
            return web
        web = run(scenario())
        assert len(agents[0].calls) == 1 and len(web.of("chat_postMessage")) == 1

    def test_control_commands_work_through_a_mention(self):
        async def scenario():
            runner, adapter, web, socket = self._rig()
            await adapter._on_request(socket, request(payload(
                type="app_mention", channel="C1", channel_type="channel", ts="700.1", text=f"<@{BOT}> !status")))
            await wait_for(lambda: web.of("chat_postMessage"))
            await wait_for(lambda: not adapter._tasks)
            return web
        assert "Idle" in run(scenario()).of("chat_postMessage")[0]["text"]


class TestApprovalButtons:
    """The question is posted with buttons; a click identifies one request."""

    def _request(self):
        from gyrfalcon.gateway.approval import ApprovalRequest
        return ApprovalRequest(tool="terminal", reason="APPROVAL_REQUIRED: Elevated privileges",
                               command="sudo systemctl restart nginx", session_key="slack:C1:700.1")

    def test_the_prompt_carries_buttons_and_the_request_id(self):
        async def scenario():
            adapter, web, _ = connected()
            ok = await adapter.ask_approval(self._request(), channel_source())
            return ok, web
        ok, web = run(scenario())
        assert ok is True
        (post,) = web.of("chat_postMessage")
        assert post["thread_ts"] == "700.1" and post["channel"] == "C1"
        actions = [b for b in post["blocks"] if b["type"] == "actions"][0]
        ids = {e["action_id"]: e for e in actions["elements"]}
        assert set(ids) == {"gyrfalcon_approve", "gyrfalcon_deny"}
        assert all(e["value"] for e in ids.values()), "each button names the request"
        assert ids["gyrfalcon_approve"]["value"] == ids["gyrfalcon_deny"]["value"]

    def test_there_is_a_text_fallback_for_notifications(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.ask_approval(self._request(), channel_source())
            return web
        post = run(scenario()).of("chat_postMessage")[0]
        assert "Approval needed" in post["text"]

    def test_the_command_is_shown_and_cannot_ping_the_channel(self):
        from gyrfalcon.gateway.approval import ApprovalRequest

        async def scenario():
            adapter, web, _ = connected()
            req = ApprovalRequest(tool="terminal", reason="APPROVAL_REQUIRED: x",
                                  command="echo <!channel> && sudo reboot", session_key="k")
            await adapter.ask_approval(req, channel_source())
            return web
        text = json.dumps(run(scenario()).of("chat_postMessage")[0]["blocks"])
        assert "sudo reboot" in text and "<!channel>" not in text

    def test_approving_needs_a_second_confirmation(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.ask_approval(self._request(), channel_source())
            return web
        actions = [b for b in run(scenario()).of("chat_postMessage")[0]["blocks"] if b["type"] == "actions"][0]
        approve = [e for e in actions["elements"] if e["action_id"] == "gyrfalcon_approve"][0]
        assert "confirm" in approve, "a misclick should not run a dangerous command"

    def test_a_failure_to_ask_is_reported_as_not_asked(self):
        async def scenario():
            web = FakeWeb()
            web.fail["chat_postMessage"] = api_error("invalid_blocks")
            adapter, _, _ = connected(web=web)
            return await adapter.ask_approval(self._request(), channel_source())
        assert run(scenario()) is False

    def test_a_click_reaches_the_runner_with_who_clicked(self):
        async def scenario():
            adapter, _, socket = connected()
            seen = []
            adapter.on_decision(lambda d: seen.append(d) or asyncio.sleep(0))
            await adapter._on_request(socket, request({
                "type": "block_actions",
                "user": {"id": "U1"},
                "container": {"channel_id": "C1"},
                "actions": [{"action_id": "gyrfalcon_approve", "value": "req-123"}],
            }, type="interactive", envelope="env-i"))
            await wait_for(lambda: seen)
            return seen[0], socket
        decision, socket = run(scenario())
        assert (decision.request_id, decision.approved, decision.user_id) == ("req-123", True, "U1")
        assert decision.chat_id == "C1" and socket.acks == ["env-i"]

    def test_a_deny_click_is_a_denial(self):
        async def scenario():
            adapter, _, socket = connected()
            seen = []
            adapter.on_decision(lambda d: seen.append(d) or asyncio.sleep(0))
            await adapter._on_request(socket, request({
                "type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "D1"},
                "actions": [{"action_id": "gyrfalcon_deny", "value": "req-9"}],
            }, type="interactive"))
            await wait_for(lambda: seen)
            return seen[0]
        assert run(scenario()).approved is False

    def test_other_interactions_are_ignored(self):
        async def scenario():
            adapter, _, socket = connected()
            seen = []
            adapter.on_decision(lambda d: seen.append(d) or asyncio.sleep(0))
            for actions in ([{"action_id": "something_else", "value": "x"}], []):
                await adapter._on_request(socket, request({
                    "type": "block_actions", "user": {"id": "U1"}, "actions": actions,
                }, type="interactive"))
            await asyncio.sleep(0.05)
            return seen
        assert run(scenario()) == []

    def test_settling_replaces_the_buttons(self):
        async def scenario():
            adapter, web, _ = connected()
            req = self._request()
            await adapter.ask_approval(req, channel_source())
            await adapter.settle_approval(req.id, "*Approved* by <@U1>")
            return web
        web = run(scenario())
        (update,) = web.of("chat_update")
        assert update["blocks"] == [], "the buttons are gone, so they cannot be clicked again"
        assert "Approved" in update["text"]

    def test_settling_an_unknown_request_does_nothing(self):
        async def scenario():
            adapter, web, _ = connected()
            await adapter.settle_approval("never-asked", "x")
            return web
        assert run(scenario()).of("chat_update") == []


class TestRegistration:
    def test_slack_resolves_through_the_registry(self):
        from gyrfalcon.gateway.platforms import load_adapter_class
        assert load_adapter_class("slack") is SlackAdapter

    def test_a_missing_sdk_says_how_to_install_it(self, monkeypatch):
        import importlib

        from gyrfalcon.gateway.platforms import AdapterUnavailable, load_adapter_class

        def fail(name, *a, **k):
            raise ImportError("No module named 'slack_sdk'")
        monkeypatch.setattr(importlib, "import_module", fail)
        with pytest.raises(AdapterUnavailable, match=r"uv sync --extra slack"):
            load_adapter_class("slack")

    def test_the_platform_name(self):
        assert SlackAdapter.platform_name == "slack"
