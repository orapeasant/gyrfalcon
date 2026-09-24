"""Teams activity mapping, outbound thread routing, and the SDK's auth boundary."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

pytest.importorskip("microsoft_teams.apps", reason="install with: uv sync --extra teams")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from microsoft_teams.api import MessageActivity  # noqa: E402
from microsoft_teams.apps import App, FastAPIAdapter  # noqa: E402

from gyrfalcon.gateway.config import PlatformConfig  # noqa: E402
from gyrfalcon.gateway.platforms.teams import TeamsAdapter  # noqa: E402

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
CHANNEL = "19:channel@thread.tacv2"
BOT = f"28:{CLIENT_ID}"


def adapter() -> TeamsAdapter:
    a = TeamsAdapter(PlatformConfig.from_config("teams", {
        "allow": {"users": ["29:alice"], "channels": [CHANNEL]},
    }))
    a._client_id = CLIENT_ID
    a._tenant_id = TENANT_ID
    return a


def activity(*, message_id="100", conversation_id=f"{CHANNEL};messageid=100", text="<at>Gyrfalcon</at> hello",
             mentioned=True, user="29:alice", tenant=TENANT_ID, conversation_type="channel") -> MessageActivity:
    return MessageActivity.model_validate({
        "type": "message", "id": message_id, "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
        "from": {"id": user, "name": "Alice"},
        "recipient": {"id": BOT, "name": "Gyrfalcon"},
        "conversation": {"id": conversation_id, "conversationType": conversation_type, "tenantId": tenant},
        "channelData": {"tenant": {"id": tenant}, "channel": {"id": CHANNEL, "name": "Ops"}},
        "text": text,
        "entities": ([{"type": "mention", "mentioned": {"id": BOT, "name": "Gyrfalcon"},
                      "text": "<at>Gyrfalcon</at>"}] if mentioned else []),
    })


def test_channel_mention_and_follow_up_share_one_session():
    a = adapter()
    first = a._to_event(activity())
    follow_up = a._to_event(activity(message_id="101", text="and then?", mentioned=False))

    assert first.text == "hello"
    assert first.source.session_key == follow_up.source.session_key == f"teams:{CHANNEL}:100"
    assert first.source.guild_id == TENANT_ID
    assert first.source.chat_type == "channel"
    assert a.config.allow.permits(first.source.user_id, first.source.chat_id, first.source.chat_type)


def test_new_channel_thread_without_mention_is_ignored():
    a = adapter()
    assert a._to_event(activity(message_id="200", conversation_id=f"{CHANNEL};messageid=200",
                                mentioned=False, text="unrelated")) is None


def test_null_thread_response_setting_is_ignored_safely():
    a = TeamsAdapter(PlatformConfig.from_config("teams", {"respond_to": None}))
    a._client_id = CLIENT_ID
    a._tenant_id = TENANT_ID
    assert a._to_event(activity(mentioned=False, text="unrelated")) is None


def test_personal_chat_needs_no_mention_and_has_rolling_session():
    a = adapter()
    first = a._to_event(activity(conversation_id="a:personal", conversation_type="personal",
                                 mentioned=False, text="hi"))
    second = a._to_event(activity(message_id="101", conversation_id="a:personal",
                                  conversation_type="personal", mentioned=False, text="again"))
    assert first.source.chat_type == "dm"
    assert first.source.session_key == second.source.session_key == "teams:a:personal:"


def test_other_tenant_and_bot_echo_are_rejected():
    a = adapter()
    assert a._to_event(activity(tenant="33333333-3333-3333-3333-333333333333")) is None
    assert a._to_event(activity(user=BOT)) is None


def test_channel_reply_and_stream_edit_keep_the_thread():
    class FakeApp:
        def __init__(self):
            self.calls = []

        async def reply(self, *args, **kwargs):
            self.calls.append(("reply", args, kwargs))
            return SimpleNamespace(id="501")

        async def send(self, *args, **kwargs):
            self.calls.append(("send", args, kwargs))
            return SimpleNamespace(id="501")

    async def scenario():
        a = adapter()
        app = FakeApp()
        a._app = app
        inbound = a._to_event(activity())
        result = await a.send(CHANNEL, "initial", reply_to="100", metadata={"source": inbound.source})
        edited = await a.edit_message(CHANNEL, result.message_id, "final")
        return result, edited, app.calls

    result, edited, calls = asyncio.run(scenario())
    assert result.success and result.message_id == "501" and edited
    assert calls[0][0] == "reply" and calls[0][1][:2] == (CHANNEL, "100")
    assert calls[0][1][2].text == "initial"
    assert calls[1][0] == "send"
    assert calls[1][1][0] == f"{CHANNEL};messageid=100"
    assert calls[1][1][1].id == "501" and calls[1][1][1].text == "final"


def test_sent_message_ids_are_scoped_to_their_chat():
    async def scenario():
        a = adapter()
        a._app = SimpleNamespace()
        a._sent[(CHANNEL, "501")] = (f"{CHANNEL};messageid=100", None)
        return await a.edit_message("another-chat", "501", "wrong chat")

    assert asyncio.run(scenario()) is False


def test_sdk_rejects_unauthenticated_http_before_dispatch():
    async def scenario():
        http_adapter = FastAPIAdapter()
        app = App(client_id=CLIENT_ID, client_secret="fake-secret", tenant_id=TENANT_ID,
                  http_server_adapter=http_adapter)
        seen = []

        @app.on_message
        async def on_message(ctx):
            seen.append(ctx.activity)

        await app.initialize()
        async with AsyncClient(transport=ASGITransport(app=http_adapter.app), base_url="http://test") as client:
            response = await client.post("/api/messages", json=activity().model_dump(by_alias=True))
        return response.status_code, seen

    status, seen = asyncio.run(scenario())
    assert status == 401 and seen == []


def test_gateway_adapter_serves_authenticated_endpoint_lifecycle(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("TEAMS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TEAMS_TENANT_ID", TENANT_ID)
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "fake-secret")

    async def scenario():
        a = TeamsAdapter(PlatformConfig.from_config("teams", {"port": port}))
        connected = await a.connect()
        try:
            if not connected:
                return connected, None
            async with AsyncClient() as client:
                response = await client.post(f"http://127.0.0.1:{port}/api/messages", json={"type": "message"})
            return connected, response.status_code
        finally:
            await a.disconnect()

    assert asyncio.run(scenario()) == (True, 401)
