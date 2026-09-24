"""Slack adapter — Socket Mode.

Spec 18-slack.md. Socket Mode rather than the Events API because this runs
behind NAT with no public URL, and because it removes request-signature
verification from the attack surface entirely. `slack_sdk` is used directly
rather than Bolt: Bolt brings its own listener registry and web server, which
would duplicate the gateway and make Slack structurally unlike every adapter
after it.

The adapter translates and does nothing else. Who may talk to the agent, rate
limiting, queueing behind a running turn, control commands and toolset choice
are the runner's job (`gateway/run.py`), done once for every platform. What is
Slack-specific, and lives here:

* **Ack first, work later.** Slack redelivers any event not acknowledged within
  three seconds, and a turn can run for minutes. The listener acknowledges and
  hands the event to a task; it never awaits the agent, or it would also stall
  the websocket's own reads.
* **Never react to ourselves.** The bot's own replies arrive as events. Without
  the filter it answers itself forever, at API speed, on the user's token bill.
* **One message, one run.** A mention in a channel arrives twice — as `message`
  and as `app_mention` — with different event ids. Runs are keyed on
  `channel:ts`, and plain `message` events that mention us are left to the
  `app_mention` copy.
* **Nothing Slack would parse leaves unescaped** (`_mrkdwn.py`): `<!channel>` in
  model output would otherwise page a whole workspace.
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from typing import Any, Callable, Optional

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from gyrfalcon.gateway.config import PlatformConfig
from gyrfalcon.gateway.platforms._mrkdwn import (
    DEFAULT_LIMIT,
    markdown_to_mrkdwn,
    slack_to_plain,
    split_message,
    strip_bot_mention,
)
from gyrfalcon.gateway.platforms.base import (
    DONE,
    FAILED,
    QUEUED,
    WORKING,
    ApprovalInteraction,
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
    SessionSource,
)
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("gateway.slack")

#: Message subtypes worth handling. Everything else — edits, deletions, joins,
#: bot messages — is noise or a loop hazard.
_HANDLED_SUBTYPES = frozenset({None, "file_share", "thread_broadcast"})

_REACTIONS = {
    QUEUED: "eyes",
    WORKING: "hourglass_flowing_sand",
    DONE: "white_check_mark",
    FAILED: "x",
}

#: Reaction errors that mean "already in the state we wanted" or "not worth
#: retrying" — anything else is logged.
_BENIGN_REACTION_ERRORS = frozenset({"already_reacted", "no_reaction", "message_not_found", "not_reactable"})

_MAX_TRACKED = 4096


def _bounded_add(store: "OrderedDict[Any, Any]", key: Any, value: Any = None) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > _MAX_TRACKED:
        store.popitem(last=False)


def _api_error(exc: Exception) -> str:
    """A short, token-free description of a Slack failure.

    Called from inside `except` blocks, so it must not raise. The obvious
    `exc.response.get("error")` does: on a non-200 reply (a 502 from a proxy, an
    HTML error page) the SDK's `response.data` is a *string*, and `.get` on it
    raises AttributeError — which would turn a failed send into a crash and a
    dropped inbound message. Errors are described by Slack's own error code when
    there is one, else by HTTP status, else by exception type; never by message
    text, which can carry URLs and tokens.
    """
    if isinstance(exc, SlackApiError):
        response = getattr(exc, "response", None)
        try:
            data = getattr(response, "data", response)
            code = data.get("error") if isinstance(data, dict) else None
            if code:
                return str(code)
            status = getattr(response, "status_code", None)
            if status:
                return f"http_{status}"
        except Exception:  # a formatter must never be the thing that fails
            pass
        return "slack_api_error"
    return type(exc).__name__


class SlackAdapter(BasePlatformAdapter):
    platform_name = "slack"

    def __init__(
        self,
        config: PlatformConfig,
        *,
        web_client: Optional[AsyncWebClient] = None,
        socket_factory: Optional[Callable[[str, AsyncWebClient], Any]] = None,
    ):
        super().__init__(config)
        self._web = web_client
        self._socket_factory = socket_factory or self._default_socket
        self._socket: Any = None
        self._tokens: dict[str, str] = {}
        self._bot_user_id = ""
        self._team_id = ""
        self._tasks: set[asyncio.Task] = set()
        #: (channel, thread_ts) pairs we have replied in — the threads in which
        #: a follow-up needs no @mention.
        self._owned_threads: "OrderedDict[tuple[str, str], None]" = OrderedDict()
        self._chat_names: "OrderedDict[str, str]" = OrderedDict()
        self._reactions: "OrderedDict[tuple[str, str], str]" = OrderedDict()
        #: request id -> (channel, ts) of the message asking about it.
        self._approval_messages: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._warned: set[str] = set()

    @property
    def _api(self) -> AsyncWebClient:
        """The Web API client, or a clear error if the adapter never connected.
        Raised inside the callers' `try` blocks, so a send before connect is a
        failed result rather than an AttributeError on None."""
        if self._web is None:
            raise RuntimeError("Slack adapter is not connected")
        return self._web

    # -- connection -----------------------------------------------------------

    @staticmethod
    def _default_socket(app_token: str, web: AsyncWebClient) -> SocketModeClient:
        return SocketModeClient(app_token=app_token, web_client=web)

    def _resolve_token(self, kind: str) -> str:
        """Token from the secret store (by configured name), else the environment.

        Never from `config.yaml` itself: the dashboard's config API returns that
        file, so a token written there is a token served over HTTP.
        """
        name = self.config.get(f"{kind}_token_secret")
        value = ""
        if name:
            from gyrfalcon.security import get_stored_secret

            value = get_stored_secret(str(name)) or ""
        return value or os.environ.get(f"SLACK_{kind.upper()}_TOKEN", "").strip()

    async def connect(self) -> bool:
        for inline in ("bot_token", "app_token"):
            if self.config.get(inline):
                logger.warning(
                    f"Ignoring '{inline}' in config.yaml — tokens are not read from there. Store it with the "
                    f"secret store and set {inline}_secret to its name, or set SLACK_{inline.upper()} in .env."
                )

        if str(self.config.get("mode", "socket")).lower() != "socket":
            logger.error("Slack: only mode 'socket' is implemented.")
            return False

        bot, app = self._resolve_token("bot"), self._resolve_token("app")
        if not bot or not app:
            missing = " and ".join(k for k, v in (("bot token", bot), ("app-level token", app)) if not v)
            logger.error(
                f"Slack: no {missing}. Store them in the secret store and set bot_token_secret / app_token_secret "
                "under gateway.platforms.slack, or set SLACK_BOT_TOKEN / SLACK_APP_TOKEN in .env."
            )
            return False
        if not bot.startswith("xoxb-"):
            logger.error("Slack: the bot token must start with 'xoxb-' (Install App > Bot User OAuth Token).")
            return False
        if not app.startswith("xapp-"):
            logger.error("Slack: the app-level token must start with 'xapp-' (Basic Information > App-Level Tokens).")
            return False
        self._tokens = {"bot": bot, "app": app}

        if self._web is None:
            self._web = AsyncWebClient(token=bot)
            self._web.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=2))

        try:
            auth = await self._web.auth_test()
        except Exception as exc:
            logger.error(f"Slack: auth.test failed ({_api_error(exc)}) — is the bot token valid and the app installed?")
            return False
        self._bot_user_id = str(auth.get("user_id", ""))
        self._team_id = str(auth.get("team_id", ""))
        if not self._bot_user_id:
            logger.error("Slack: auth.test returned no bot user id.")
            return False

        self._socket = self._socket_factory(app, self._web)
        self._socket.socket_mode_request_listeners.append(self._on_request)
        try:
            await self._socket.connect()
        except Exception as exc:
            logger.error(f"Slack: Socket Mode connection failed ({_api_error(exc)}) — check the app-level token "
                         "has the connections:write scope and Socket Mode is enabled.")
            return False

        logger.info(f"Slack connected as {auth.get('user', self._bot_user_id)} in workspace {self._team_id}")
        return True

    async def disconnect(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._socket is not None:
            try:
                await self._socket.disconnect()
                await self._socket.close()
            except Exception:
                logger.debug("Slack socket close failed", exc_info=True)
            self._socket = None

    def secrets(self) -> list[str]:
        return [v for v in self._tokens.values() if v]

    # -- inbound --------------------------------------------------------------

    async def _on_request(self, client: Any, req: SocketModeRequest) -> None:
        """Socket Mode listener. Acknowledge, then get out of the way."""
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.warning("Slack: failed to acknowledge envelope", exc_info=True)

        if req.type == "interactive":
            await self._on_interaction(req.payload or {})
            return
        if req.type != "events_api":
            return  # slash commands are not handled yet

        # A task, never an await: the agent may run for minutes, and awaiting it
        # here would stall this listener and with it the socket's own reads.
        task = asyncio.create_task(self._process(req.payload or {}))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, payload: dict) -> None:
        try:
            event = await self._to_event(payload.get("event") or {}, payload)
            if event is not None:
                await self.dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Slack: error handling event")

    async def _to_event(self, event: dict, payload: dict) -> Optional[MessageEvent]:
        etype = event.get("type")
        if etype not in ("message", "app_mention"):
            return None
        if event.get("subtype") not in _HANDLED_SUBTYPES:
            return None
        user, channel, ts = event.get("user"), event.get("channel"), event.get("ts")
        if not (user and channel and ts):
            return None
        # Never answer ourselves, or any other bot: the loop that results burns
        # tokens at API speed.
        if event.get("bot_id") or user == self._bot_user_id:
            return None

        ctype = event.get("channel_type") or {"D": "im", "G": "group"}.get(channel[:1], "channel")
        is_dm = ctype == "im"
        raw = event.get("text") or ""
        thread_ts = event.get("thread_ts")
        respond = self.config.get("respond_to") or {}

        if is_dm:
            if not respond.get("dms", True):
                return None
        elif etype == "app_mention":
            if not respond.get("mentions", True):
                return None
        else:
            # A plain message in a shared space. If it mentions us, its
            # app_mention twin carries it — handling both would run it twice.
            if f"<@{self._bot_user_id}" in raw:
                return None
            if not (respond.get("threads", True) and thread_ts and self._owns_thread(channel, thread_ts)):
                return None

        text = slack_to_plain(strip_bot_mention(raw, self._bot_user_id)).strip()
        files = event.get("files") or []
        if files:
            note = f"[{len(files)} attachment(s) were shared, which I cannot read yet.]"
            text = f"{text}\n\n{note}".strip()

        # A DM has no threads unless the user starts one: one rolling conversation
        # per DM, rather than a fresh context for every message.
        thread_id = thread_ts or ("" if is_dm else ts)
        if not is_dm:
            _bounded_add(self._owned_threads, (channel, thread_id))

        source = SessionSource(
            platform="slack",
            chat_id=channel,
            chat_name="" if is_dm else await self._chat_name(channel),
            user_id=user,
            thread_id=thread_id,
            chat_type="dm" if is_dm else ("group" if ctype == "mpim" else "channel"),
            guild_id=str(payload.get("team_id") or self._team_id),
            message_id=ts,
        )
        # Keyed on the message, not Slack's event id: a mention is delivered as
        # two events, and a redelivery reuses neither.
        return MessageEvent(source=source, text=text, event_id=f"{channel}:{ts}")

    def _owns_thread(self, channel: str, thread_ts: str) -> bool:
        return (channel, thread_ts) in self._owned_threads

    async def _chat_name(self, channel: str) -> str:
        """Channel name for routing rules; empty if it cannot be had (rules can
        still match on the channel id)."""
        if channel in self._chat_names:
            return self._chat_names[channel]
        name = ""
        try:
            info = await self._api.conversations_info(channel=channel)
            name = str((info.get("channel") or {}).get("name") or "")
        except Exception as exc:
            self._warn_once("conversations_info", f"Slack: cannot read channel names ({_api_error(exc)}); "
                            "routing rules can still match on channel ids. Add the channels:read / groups:read scopes.")
        _bounded_add(self._chat_names, channel, name)
        return name

    # -- outbound -------------------------------------------------------------

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict | None = None
    ) -> SendResult:
        source: Optional[SessionSource] = (metadata or {}).get("source")
        thread_ts = reply_to
        if source is not None and source.chat_type == "dm" and not source.thread_id:
            thread_ts = None  # a DM is a conversation, not a pile of threads

        first_ts: Optional[str] = None
        for chunk in split_message(markdown_to_mrkdwn(content)):
            try:
                resp = await self._api.chat_postMessage(
                    channel=chat_id, text=chunk, thread_ts=thread_ts,
                    mrkdwn=True, parse="none", link_names=False, unfurl_links=False, unfurl_media=False,
                )
            except Exception as exc:
                return SendResult(success=False, message_id=first_ts, error=_api_error(exc))
            first_ts = first_ts or resp.get("ts")

        if thread_ts:
            _bounded_add(self._owned_threads, (chat_id, thread_ts))
        return SendResult(success=True, message_id=first_ts)

    async def edit_message(self, chat_id: str, message_id: str, content: str) -> bool:
        text = markdown_to_mrkdwn(content)
        if len(text) > DEFAULT_LIMIT:
            text = text[: DEFAULT_LIMIT - 1] + "…"
        try:
            await self._api.chat_update(channel=chat_id, ts=message_id, text=text, parse="none", link_names=False)
            return True
        except Exception as exc:
            logger.debug(f"Slack: chat.update failed ({_api_error(exc)})")
            return False

    async def get_chat_info(self, chat_id: str) -> dict:
        try:
            info = await self._api.conversations_info(channel=chat_id)
        except Exception as exc:
            return {"id": chat_id, "error": _api_error(exc)}
        ch = info.get("channel") or {}
        return {
            "id": chat_id, "name": ch.get("name", ""), "is_im": bool(ch.get("is_im")),
            "is_private": bool(ch.get("is_private")), "num_members": ch.get("num_members"),
        }

    # -- approvals -------------------------------------------------------------

    async def ask_approval(self, request: Any, source: SessionSource) -> bool:
        """Post the question with Approve / Deny buttons.

        `blocks` carries the buttons; `text` is the notification fallback and
        what a client that cannot render blocks shows. The request id travels in
        each button's `value`, so a click identifies exactly one question even
        if several are open — and a stale button from an old message resolves to
        nothing rather than to whatever is pending now.
        """
        command = f"```\n{markdown_to_mrkdwn(request.command)}\n```\n" if request.command else ""
        blocks: list[dict] = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Approval needed* — {markdown_to_mrkdwn(request.summary())}\n{command}",
            }},
            {"type": "actions", "block_id": f"gyrfalcon_approval:{request.id}", "elements": [
                {"type": "button", "action_id": "gyrfalcon_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}, "value": request.id,
                 "confirm": {
                     "title": {"type": "plain_text", "text": "Run it?"},
                     "text": {"type": "mrkdwn", "text": "This runs on the machine hosting the agent."},
                     "confirm": {"type": "plain_text", "text": "Run it"},
                     "deny": {"type": "plain_text", "text": "Cancel"},
                 }},
                {"type": "button", "action_id": "gyrfalcon_deny", "style": "danger",
                 "text": {"type": "plain_text", "text": "Deny"}, "value": request.id},
            ]},
        ]
        try:
            resp = await self._api.chat_postMessage(
                channel=source.chat_id,
                text=f"Approval needed: {request.summary()}",
                blocks=blocks,
                thread_ts=source.thread_id or source.message_id or None,
                parse="none", link_names=False,
            )
        except Exception as exc:
            code = _api_error(exc)
            hint = " — enable Interactivity in the app's settings." if code == "invalid_blocks" else ""
            self._warn_once(f"approval:{code}", f"Slack: could not ask for approval ({code}){hint}")
            return False
        _bounded_add(self._approval_messages, request.id, (source.chat_id, resp.get("ts")))
        return True

    async def _on_interaction(self, payload: dict) -> None:
        """A button was clicked. Hand the decision to the runner, which decides
        whether this person is allowed to make it."""
        actions = payload.get("actions") or []
        if not actions:
            return
        action = actions[0]
        action_id = action.get("action_id") or ""
        if action_id not in ("gyrfalcon_approve", "gyrfalcon_deny"):
            return
        request_id = str(action.get("value") or "")
        user = ((payload.get("user") or {}).get("id")) or ""
        channel = ((payload.get("container") or {}).get("channel_id")) or \
                  ((payload.get("channel") or {}).get("id")) or ""
        if not (request_id and user):
            return

        if self._decision_callback is not None:
            await self._decision_callback(ApprovalInteraction(
                request_id=request_id,
                approved=action_id == "gyrfalcon_approve",
                user_id=user,
                chat_id=channel,
                platform="slack",
            ))

    async def settle_approval(self, request_id: str, text: str) -> None:
        """Replace the question with what was decided, so the buttons cannot be
        clicked again and the thread records the outcome."""
        where = self._approval_messages.pop(request_id, None)
        if not where:
            return
        chat_id, ts = where
        try:
            await self._api.chat_update(channel=chat_id, ts=ts, text=markdown_to_mrkdwn(text),
                                        blocks=[], parse="none", link_names=False)
        except Exception as exc:
            logger.debug(f"Slack: could not settle approval message ({_api_error(exc)})")

    async def acknowledge(self, event: MessageEvent, state: str) -> None:
        """Show progress as a reaction on the user's message. Best effort."""
        wanted = _REACTIONS.get(state)
        src = event.source
        if not wanted or not src.message_id:
            return
        key = (src.chat_id, src.message_id)
        previous = self._reactions.get(key)
        if previous == wanted:
            return
        if previous:
            await self._react("reactions_remove", src, previous)
        await self._react("reactions_add", src, wanted)
        _bounded_add(self._reactions, key, wanted)

    async def _react(self, method: str, src: SessionSource, name: str) -> None:
        try:
            await getattr(self._api, method)(channel=src.chat_id, timestamp=src.message_id, name=name)
        except Exception as exc:
            code = _api_error(exc)
            if code not in _BENIGN_REACTION_ERRORS:
                hint = " — add the reactions:write scope to show progress." if code == "missing_scope" else ""
                self._warn_once(f"reaction:{code}", f"Slack: reaction failed ({code}){hint}")

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)
