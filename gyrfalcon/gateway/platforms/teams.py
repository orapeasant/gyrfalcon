"""Microsoft Teams adapter using the authenticated Teams SDK HTTP endpoint.

The SDK validates Bot Service JWTs before invoking ``on_message``. The handler
returns promptly; agent turns run through GatewayRunner after the HTTP response
has been acknowledged. A Teams channel thread maps to one gateway session.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
from collections import OrderedDict
from typing import Any

import uvicorn
from microsoft_teams.api import MessageActivityInput
from microsoft_teams.apps import App, FastAPIAdapter

from gyrfalcon.gateway.config import PlatformConfig
from gyrfalcon.gateway.platforms._mrkdwn import split_message
from gyrfalcon.gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult, SessionSource
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("gateway.teams")

_THREAD_SUFFIX = re.compile(r";messageid=([^;]+)", re.IGNORECASE)
_MAX_REFERENCES = 2048
_START_TIMEOUT_SECONDS = 10.0


def _base_conversation_id(conversation_id: str) -> str:
    return conversation_id.split(";", 1)[0]


def _thread_root(conversation_id: str, reply_to_id: str | None, message_id: str) -> str:
    match = _THREAD_SUFFIX.search(conversation_id)
    return match.group(1) if match else (reply_to_id or message_id)


def _remember(mapping: OrderedDict, key: Any, value: Any) -> None:
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > _MAX_REFERENCES:
        mapping.popitem(last=False)


class TeamsAdapter(BasePlatformAdapter):
    platform_name = "teams"

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self._app: App | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._client_id = ""
        self._tenant_id = ""
        self._client_secret = ""
        self._service_urls: OrderedDict[str, str] = OrderedDict()
        self._owned_threads: OrderedDict[tuple[str, str], bool] = OrderedDict()
        self._sent: OrderedDict[tuple[str, str], tuple[str, str | None]] = OrderedDict()

    def _credentials(self) -> tuple[str, str, str]:
        if self.config.get("client_secret"):
            logger.warning("Ignoring client_secret in config.yaml; use TEAMS_CLIENT_SECRET or client_secret_secret.")
        client_id = str(self.config.get("client_id") or os.environ.get("TEAMS_CLIENT_ID") or "").strip()
        tenant_id = str(self.config.get("tenant_id") or os.environ.get("TEAMS_TENANT_ID") or "").strip()
        secret = ""
        secret_name = self.config.get("client_secret_secret")
        if secret_name:
            from gyrfalcon.security import get_stored_secret

            secret = get_stored_secret(str(secret_name)) or ""
        secret = secret or os.environ.get("TEAMS_CLIENT_SECRET", "")
        return client_id, tenant_id, secret.strip()

    async def connect(self) -> bool:
        client_id, tenant_id, secret = self._credentials()
        if not all((client_id, tenant_id, secret)):
            logger.error(
                "Teams: client ID, tenant ID, and client secret are required. "
                "Set TEAMS_CLIENT_ID, TEAMS_TENANT_ID, TEAMS_CLIENT_SECRET in the profile .env."
            )
            return False

        try:
            port = int(self.config.get("port", 3978))
            if not 1 <= port <= 65535:
                raise ValueError("port outside 1..65535")
        except (TypeError, ValueError):
            logger.error("Teams: port must be an integer from 1 to 65535")
            return False
        host = str(self.config.get("host", "127.0.0.1")).strip()
        if not host:
            logger.error("Teams: host must be nonempty")
            return False

        self._client_id, self._tenant_id, self._client_secret = client_id, tenant_id, secret

        def make_server(asgi_app):
            self._server = uvicorn.Server(uvicorn.Config(asgi_app, host=host, port=port, log_level="warning"))
            return self._server

        try:
            http_adapter = FastAPIAdapter(server_factory=make_server)
            app = App(
                client_id=client_id,
                client_secret=secret,
                tenant_id=tenant_id,
                http_server_adapter=http_adapter,
                messaging_endpoint="/api/messages",
            )

            @app.on_message
            async def on_message(ctx):
                event = self._to_event(ctx.activity)
                if event is not None:
                    self._schedule_dispatch(event)

            self._app = app
            await app.initialize()
            self._server_task = asyncio.create_task(app.start(port))
            for _ in range(int(_START_TIMEOUT_SECONDS / 0.05)):
                if self._server and self._server.started:
                    logger.info("Teams listening on %s:%s/api/messages (tenant %s)", host, port, tenant_id)
                    return True
                if self._server_task.done():
                    self._server_task.result()
                    logger.error("Teams: HTTP server stopped during startup")
                    break
                await asyncio.sleep(0.05)
            else:
                logger.error("Teams: HTTP server did not start within %.0fs", _START_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Teams: failed to start authenticated messaging endpoint")

        await self.disconnect()
        return False

    async def disconnect(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._app is not None:
            try:
                await self._app.stop()
            except Exception:
                logger.debug("Teams app stop failed", exc_info=True)
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5)
            except Exception:
                self._server_task.cancel()
                await asyncio.gather(self._server_task, return_exceptions=True)
        self._server_task = None
        self._server = None
        self._app = None

    def _schedule_dispatch(self, event: MessageEvent) -> None:
        task = asyncio.create_task(self.dispatch(event))
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                try:
                    done.result()
                except Exception:
                    logger.exception("Teams: error dispatching message")

        task.add_done_callback(finished)

    def _to_event(self, activity: Any) -> MessageEvent | None:
        conversation = getattr(activity, "conversation", None)
        sender = getattr(activity, "from_", None)
        recipient = getattr(activity, "recipient", None)
        message_id = str(getattr(activity, "id", "") or "")
        raw_conversation_id = str(getattr(conversation, "id", "") or "")
        user_id = str(getattr(sender, "id", "") or "")
        if not (raw_conversation_id and user_id and message_id):
            return None
        if user_id in {self._client_id, f"28:{self._client_id}", str(getattr(recipient, "id", "") or "")}:
            return None

        channel_data = getattr(activity, "channel_data", None)
        tenant = getattr(channel_data, "tenant", None)
        tenant_id = str(getattr(tenant, "id", "") or getattr(conversation, "tenant_id", "") or "")
        if tenant_id != self._tenant_id:
            logger.warning("Teams: dropped message with missing or unexpected tenant")
            return None

        chat_id = _base_conversation_id(raw_conversation_id)
        conversation_type = str(getattr(conversation, "conversation_type", "") or "").lower()
        is_dm = conversation_type == "personal" or chat_id.startswith("a:")
        chat_type = "dm" if is_dm else ("group" if conversation_type == "groupchat" else "channel")
        thread_id = "" if is_dm else _thread_root(
            raw_conversation_id, getattr(activity, "reply_to_id", None), message_id
        )

        mentioned = bool(getattr(activity, "is_recipient_mentioned", lambda: False)())
        if not is_dm and not mentioned:
            respond_to = self.config.get("respond_to") or {}
            if not (respond_to.get("threads", True)
                    and (chat_id, thread_id) in self._owned_threads):
                return None

        text = str(getattr(activity, "text", "") or "")
        for entity in getattr(activity, "entities", None) or []:
            if getattr(entity, "type", "") != "mention":
                continue
            mentioned_id = str(getattr(getattr(entity, "mentioned", None), "id", "") or "")
            if mentioned_id in {self._client_id, f"28:{self._client_id}", str(getattr(recipient, "id", "") or "")}:
                text = text.replace(str(getattr(entity, "text", "") or ""), "", 1)
        text = html.unescape(text).strip()
        attachments = getattr(activity, "attachments", None) or []
        if attachments:
            text = f"{text}\n\n[{len(attachments)} attachment(s) were shared, which I cannot read yet.]".strip()
        if not text:
            return None

        if not is_dm:
            _remember(self._owned_threads, (chat_id, thread_id), True)
        service_url = str(getattr(activity, "service_url", "") or "")
        if service_url.startswith("https://"):
            _remember(self._service_urls, chat_id, service_url)

        channel = getattr(channel_data, "channel", None)
        source = SessionSource(
            platform="teams",
            chat_id=chat_id,
            chat_name=str(getattr(channel, "name", "") or getattr(conversation, "name", "") or ""),
            user_id=user_id,
            user_name=str(getattr(sender, "name", "") or ""),
            thread_id=thread_id,
            chat_type=chat_type,
            guild_id=tenant_id,
            message_id=message_id,
        )
        return MessageEvent(source=source, text=text, event_id=f"{chat_id}:{message_id}")

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict | None = None
    ) -> SendResult:
        if self._app is None:
            return SendResult(False, error="Teams is not connected")
        source = (metadata or {}).get("source")
        is_channel = getattr(source, "chat_type", "") == "channel"
        service_url = self._service_urls.get(chat_id)
        chunks = split_message(content, limit=3800)
        first_id = None
        try:
            for chunk in chunks:
                activity = MessageActivityInput(text=chunk, text_format="markdown")
                if is_channel and reply_to:
                    sent = await self._app.reply(chat_id, reply_to, activity, service_url=service_url)
                    conversation_id = f"{chat_id};messageid={reply_to}"
                else:
                    sent = await self._app.send(chat_id, activity, service_url=service_url)
                    conversation_id = chat_id
                message_id = str(sent.id or "")
                if message_id:
                    first_id = first_id or message_id
                    _remember(self._sent, (chat_id, message_id), (conversation_id, service_url))
            return SendResult(True, message_id=first_id)
        except Exception as exc:
            logger.warning("Teams: send failed (%s)", type(exc).__name__)
            return SendResult(False, error=type(exc).__name__)

    async def edit_message(self, chat_id: str, message_id: str, content: str) -> bool:
        if self._app is None or (chat_id, message_id) not in self._sent:
            return False
        conversation_id, service_url = self._sent[(chat_id, message_id)]
        try:
            first_chunk = split_message(content, limit=3800)[0]
            activity = MessageActivityInput(id=message_id, text=first_chunk, text_format="markdown")
            await self._app.send(conversation_id, activity, service_url=service_url)
            return True
        except Exception:
            logger.debug("Teams: edit failed", exc_info=True)
            return False

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"id": chat_id, "platform": "teams", "connected": self._app is not None}

    def secrets(self) -> list[str]:
        return [self._client_secret] if self._client_secret else []
