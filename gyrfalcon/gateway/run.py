"""GatewayRunner — the one place messages from any platform become agent turns.

Spec 12-gateway.md and 18-slack.md. Adapters translate; this decides. Every
inbound event passes the same gates in the same order, so a new adapter inherits
all of them and cannot forget one:

    bot? → duplicate? → authorised? → rate-limited? → control command?
         → queue behind an active turn, or run

and every reply is scrubbed of credentials on the way out.

Two invariants worth stating because they are where this goes wrong:

* **The event loop must never run agent code.** `AIAgent.run_conversation` is
  synchronous and can take minutes; run inline, it stalls the platform's
  websocket, misses its heartbeats and blows the 3-second acknowledgement
  window. Turns run in a thread pool.
* **Contextvars do not cross that boundary by themselves** (CLAUDE.md records
  three earlier defects from exactly this). The turn is submitted through
  `copy_context().run`, and the principal is bound *inside* the worker.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import time
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gyrfalcon.gateway.approval import (
    DEFAULT_TIMEOUT_SECONDS as APPROVAL_TIMEOUT_SECONDS,
)
from gyrfalcon.gateway.approval import (
    ApprovalBroker,
    in_conversation,
    set_broker,
)
from gyrfalcon.gateway.config import DEFAULT_MAX_ITERATIONS, GatewayConfig
from gyrfalcon.gateway.delivery import DeliveryRouter, set_router
from gyrfalcon.gateway.platforms import AdapterUnavailable, load_adapter_class
from gyrfalcon.gateway.platforms.base import (
    DONE,
    FAILED,
    QUEUED,
    WORKING,
    ApprovalInteraction,
    BasePlatformAdapter,
    MessageEvent,
    SessionSource,
)
from gyrfalcon.gateway.principal import UnlinkedIdentityError, resolve_principal
from gyrfalcon.gateway.redact import redact_secrets
from gyrfalcon.gateway.streaming import DEFAULT_INTERVAL_SECONDS, StreamingReply
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.identity import use_principal
from gyrfalcon.toolsets import ELEVATED_DEFAULT_TOOLSET, GATEWAY_DEFAULT_TOOLSET, dangerous_tools_in

logger = get_logger("gateway")

#: Messages queued behind an active turn, per session. A cap so one allowed user
#: cannot grow memory (and the next prompt) without bound.
MAX_PENDING = 20

#: How long `stop()` waits for turns already running before closing the session
#: store underneath them. Bounded so shutdown cannot hang on a wedged turn.
SHUTDOWN_GRACE_SECONDS = 10.0

CONTROL_COMMANDS = ("stop", "reset", "status", "help", "approve", "deny")
_SESSION_META = "gateway.session:"

_HELP = (
    "Commands (prefix with `!`): `stop` interrupts the current reply, `reset` starts this "
    "thread's conversation over, `status` shows what's running, `approve` / `deny` answer a "
    "pending approval. Anything else is sent to the agent."
)


# ── small pieces ──────────────────────────────────────────────────────────────

class AgentCache:
    """LRU cache for agent sessions."""

    def __init__(self, max_size: int = 128, ttl: int = 3600, clock: Callable[[], float] = time.time):
        import threading

        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, session_key: str) -> Optional[Any]:
        with self._lock:
            if session_key in self._cache:
                agent, ts = self._cache[session_key]
                if self._clock() - ts < self._ttl:
                    # Touching an entry refreshes its idle timer: the TTL is *idle*
                    # time, and an active thread must not be evicted mid-conversation.
                    self._cache[session_key] = (agent, self._clock())
                    self._cache.move_to_end(session_key)
                    return agent
                del self._cache[session_key]
            return None

    def put(self, session_key: str, agent: Any) -> None:
        with self._lock:
            self._cache[session_key] = (agent, self._clock())
            self._cache.move_to_end(session_key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def remove(self, session_key: str) -> None:
        with self._lock:
            self._cache.pop(session_key, None)

    def size(self) -> int:
        return len(self._cache)


class _RateLimiter:
    """Sliding one-minute window per key."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[Any, deque[float]] = defaultdict(deque)

    def allow(self, key: Any, per_minute: int) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= per_minute:
            return False
        hits.append(now)
        return True


class _SeenEvents:
    """Bounded memory of recent event ids. Platforms redeliver on any ack hiccup,
    and a redelivered event is a second agent run the user pays for."""

    def __init__(self, max_size: int = 1024, ttl: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self._seen: OrderedDict[Any, float] = OrderedDict()
        self._max, self._ttl, self._clock = max_size, ttl, clock

    def seen_before(self, key: Any) -> bool:
        now = self._clock()
        while self._seen and next(iter(self._seen.values())) < now - self._ttl:
            self._seen.popitem(last=False)
        if key in self._seen:
            return True
        self._seen[key] = now
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return False


@dataclass
class _SessionState:
    busy: bool = False
    pending: list[MessageEvent] = field(default_factory=list)
    #: The agent currently mid-turn, so `!stop` can interrupt it.
    agent: Any = None
    #: Bumped by `!reset`, so a turn that was already running when the session
    #: was reset does not write its old session back into the mapping.
    epoch: int = 0


@dataclass(frozen=True)
class AgentSettings:
    toolset: str
    model: str
    max_iterations: int


def parse_control(text: str) -> Optional[tuple[str, str]]:
    """`("stop", "")` for `!stop` / `/stop`, else None.

    Only a fixed set of words counts, so `/etc/passwd` or `!important` are just
    messages. `!` is the primary prefix because Slack's composer swallows a
    leading `/` as a slash command the app never receives.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "!/":
        return None
    word, _, rest = stripped[1:].partition(" ")
    word = word.lower()
    return (word, rest.strip()) if word in CONTROL_COMMANDS else None


# ── the runner ────────────────────────────────────────────────────────────────

class GatewayRunner:
    """Manages platform adapter lifecycles and routes their messages to agents."""

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        agent_factory: Callable[[SessionSource, Optional[str], AgentSettings], Any] | None = None,
        session_db: Any = None,
        run_scheduler: bool = True,
        adapter_loader: Callable[[str], type] = load_adapter_class,
    ):
        self.config = config or GatewayConfig.load()
        self._agent_cache = AgentCache(max_size=self.config.agent_cache_size, ttl=self.config.idle_ttl)
        self._owns_db = session_db is None
        if session_db is None:
            from gyrfalcon.gyrfalcon_state import SessionDB

            session_db = SessionDB()
        self._session_db = session_db
        self._agent_factory = agent_factory or self._build_agent
        self._run_scheduler = run_scheduler
        self._adapter_loader = adapter_loader
        self._adapters: dict[str, BasePlatformAdapter] = {}
        self._states: dict[str, _SessionState] = {}
        self._recorded: dict[str, str] = {}
        self._broker: Optional[ApprovalBroker] = None
        self._approval_timeout = APPROVAL_TIMEOUT_SECONDS
        self._seen = _SeenEvents()
        self._limiter = _RateLimiter()
        #: Turns currently on a worker thread, so shutdown can wait for them.
        self._inflight: set[asyncio.Future] = set()
        self._pool = ThreadPoolExecutor(max_workers=self.config.max_concurrent_sessions, thread_name_prefix="gw-turn")
        self._stop_event: Optional[asyncio.Event] = None
        self._running = False

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Connect all platforms, start the scheduler, run until stopped."""
        self._running = True
        self._stop_event = asyncio.Event()
        logger.info("Gateway starting...")

        if self._run_scheduler:
            from gyrfalcon.scheduler import scheduler

            scheduler.start()

        self._warn_on_dangerous_toolsets()

        for name, pcfg in self.config.platforms.items():
            if not pcfg.enabled:
                continue
            try:
                adapter = self._create_adapter(name)
                if not await adapter.connect():
                    logger.warning(f"Failed to connect: {name}")
                    continue
                self._adapters[name] = adapter
                logger.info(f"Connected platform: {name}")
                if not pcfg.allow.users:
                    logger.warning(
                        f"Platform '{name}' has an empty allow.users list, so it will refuse every message. "
                        "Add the user ids that may talk to the agent under gateway.platforms.%s.allow.users.", name,
                    )
            except AdapterUnavailable as exc:
                logger.error(str(exc))
            except Exception as exc:
                logger.error(f"Platform {name} error: {exc}")

        # Published only once adapters are connected: a job delivering to a
        # platform that failed to connect should be told so, not queued.
        loop = asyncio.get_running_loop()
        set_router(DeliveryRouter(self._adapters, loop))
        self._broker = ApprovalBroker(loop, timeout=self._approval_timeout)
        set_broker(self._broker)

        logger.info(f"Gateway running with {len(self._adapters)} platform(s)")
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        logger.info("Gateway stopping...")
        set_router(None)
        if self._broker is not None:
            # Release every paused turn before waiting for them, or the wait
            # below is guaranteed to reach its timeout.
            self._broker.cancel_all()
            set_broker(None)
        if self._stop_event:
            self._stop_event.set()

        for state in self._states.values():
            state.pending.clear()
            self._interrupt(state)

        # Wait for turns already on a worker thread. Without this the session
        # store is closed while they are still writing to it: `SessionDB.close()`
        # nulls the connection and its `conn` property silently reopens one, so
        # the write survives but leaves a connection open past shutdown — and
        # `close()` racing an `execute()` on the same connection is the shape of
        # crash this codebase has hit before. Interrupting is asked first, above;
        # an agent only notices between LLM calls, hence a grace period, bounded
        # so a wedged turn cannot stop the gateway from stopping.
        await self._drain_inflight(SHUTDOWN_GRACE_SECONDS)

        for name, adapter in list(self._adapters.items()):
            try:
                await adapter.disconnect()
                logger.info(f"Disconnected: {name}")
            except Exception as exc:
                logger.error(f"Error disconnecting {name}: {exc}")

        self._pool.shutdown(wait=False, cancel_futures=True)

        if self._run_scheduler:
            from gyrfalcon.scheduler import scheduler

            scheduler.stop()

        if self._owns_db:
            self._session_db.close()
        logger.info("Gateway stopped")

    async def _drain_inflight(self, timeout: float) -> None:
        """Wait for running turns, up to `timeout`. Says so if any outlast it."""
        pending = {f for f in self._inflight if not f.done()}
        if not pending:
            return
        logger.info(f"Waiting up to {timeout:.0f}s for {len(pending)} turn(s) to finish...")
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        if still_running:
            logger.warning(
                f"{len(still_running)} turn(s) did not stop within {timeout:.0f}s; shutting down anyway. "
                "Their replies will not be delivered."
            )

    def _create_adapter(self, name: str) -> BasePlatformAdapter:
        pcfg = self.config.platforms[name]
        adapter = self._adapter_loader(name)(pcfg)
        adapter.on_message(functools.partial(self.handle_event, adapter))
        adapter.on_decision(functools.partial(self.handle_decision, adapter))
        return adapter

    def _warn_on_dangerous_toolsets(self) -> None:
        """Naming a toolset with a shell in it is the operator's call — but it
        turns the allowlist into the only thing between a chat message and that
        shell, so it is said out loud rather than left to be discovered."""
        named = [(f"platform '{n}'", p.toolset) for n, p in self.config.platforms.items() if p.enabled and p.toolset]
        named += [(f"routing rule '{r.pattern}'", r.toolset) for r in self.config.routing_rules if r.toolset]
        for where, toolset in named:
            risky = dangerous_tools_in([toolset])
            if risky:
                logger.warning(
                    f"{where} uses toolset '{toolset}', which exposes {sorted(risky)} to chat messages. "
                    "The allowlist is the only control between a message and the machine."
                )

    # -- inbound --------------------------------------------------------------

    async def handle_event(self, adapter: BasePlatformAdapter, event: MessageEvent) -> None:
        """Entry point for every inbound message from every adapter."""
        src = event.source
        pcfg = adapter.config

        if src.is_bot:
            return
        if event.event_id and self._seen.seen_before((src.platform, event.event_id)):
            logger.debug(f"Dropping redelivered event {event.event_id}")
            return

        if not pcfg.allow.permits(src.user_id, src.chat_id, src.chat_type):
            logger.warning(f"Refused {src.platform} message from unauthorised user={src.user_id} chat={src.chat_id}")
            if pcfg.reply_on_deny:
                await self._say(adapter, src, "You are not authorised to use this agent.")
            return

        if not self._limiter.allow((src.platform, src.user_id), pcfg.rate_limit_rpm):
            logger.warning(f"Rate limited {src.platform} user={src.user_id}")
            await self._say(adapter, src, "You're sending messages faster than I can handle — give it a moment.")
            return

        control = parse_control(event.text)
        if control:
            await self._handle_control(adapter, event, *control)
            return

        if not event.text.strip():
            return

        key = src.session_key
        state = self._states.setdefault(key, _SessionState())
        if state.busy:
            state.pending.append(event)
            if len(state.pending) > MAX_PENDING:
                del state.pending[0]
            await self._ack(adapter, event, QUEUED)
            return

        state.busy = True
        try:
            await self._drain(adapter, key, state, event)
        finally:
            state.busy = False
            # Forget a conversation that has gone quiet. One entry per thread,
            # kept forever, is a slow leak in a daemon that runs for months —
            # the agent cache is LRU-bounded, this was not. Nothing is lost: the
            # thread's session id lives in the store, so the next message
            # resumes it. Safe here because no `await` separates the
            # empty-queue check from this line, so no message can slip in.
            if not state.pending and state.agent is None:
                self._states.pop(key, None)
                self._recorded.pop(key, None)

    async def _drain(self, adapter: BasePlatformAdapter, key: str, state: _SessionState, event: MessageEvent) -> None:
        """Run a turn, then any messages that arrived while it ran, folded into one.

        No `await` sits between the empty-queue check and `busy` being cleared by
        the caller, so a message cannot slip into the gap and be stranded.
        """
        current, text, batch = event, event.text, [event]
        while True:
            await self._ack(adapter, current, WORKING)
            ok = True
            stream = self._begin_stream(adapter, current)
            try:
                reply = await self._run_turn(current, text, key, state, stream, adapter)
            except UnlinkedIdentityError as exc:
                logger.warning(str(exc))
                reply, ok = "I can't tell which account you are, so I can't act on that.", False
            except Exception:
                # Never echo the exception: it can carry paths, prompts or tokens.
                logger.exception(f"Agent error for {key}")
                reply, ok = "Something went wrong handling that. It has been logged.", False

            # A failed turn abandons its partial text and says so plainly, rather
            # than settling a half-written answer that reads as if it finished.
            delivered = False
            if stream is not None and ok:
                delivered = await stream.finish(reply)
            elif stream is not None:
                await stream.abandon()
            if not delivered:
                await self._deliver(adapter, current, reply)
            # Every message folded into this turn is finished, not just the last:
            # the earlier ones were marked queued and would otherwise stay so.
            for handled in batch:
                await self._ack(adapter, handled, DONE if ok else FAILED)

            if not state.pending:
                return
            batch, state.pending = state.pending, []
            current, text = batch[-1], "\n\n".join(e.text for e in batch)

    def _begin_stream(self, adapter: BasePlatformAdapter, event: MessageEvent) -> Optional[StreamingReply]:
        """A live reply for this turn, or None when the platform is set not to."""
        pcfg = self.config.platforms.get(event.source.platform)
        if pcfg is not None and not pcfg.get("stream", True):
            return None
        # A nonsense interval falls back to the default rather than being
        # clamped, matching how the rest of the config treats bad numbers.
        seconds = DEFAULT_INTERVAL_SECONDS
        try:
            configured = float(pcfg.get("stream_interval_ms")) if pcfg else 0.0
            if configured > 0:
                seconds = max(0.2, configured / 1000)
        except (TypeError, ValueError):
            pass
        stream = StreamingReply(
            adapter, event, interval=seconds,
            transform=lambda t: redact_secrets(t, adapter.secrets()),
        )
        stream.begin()
        return stream

    async def _run_turn(
        self, event: MessageEvent, text: str, key: str, state: _SessionState,
        stream: Optional[StreamingReply] = None, adapter: Optional[BasePlatformAdapter] = None,
    ) -> str:
        src = event.source
        principal = resolve_principal(src)
        epoch = state.epoch

        def _blocking() -> str:
            # Bound here, inside the worker: agent creation touches the session
            # store, which needs a principal just as the turn itself does.
            with use_principal(principal):
                agent = self._agent_cache.get(key)
                history = None
                if agent is None:
                    agent, history = self._create_agent(key, src)
                    self._agent_cache.put(key, agent)
                state.agent = agent
                # Attached per turn rather than at construction: the agent is
                # cached across turns, and turns in one session are serialised,
                # so exactly one stream is ever attached. Restored afterwards so
                # a later turn with streaming off does not inherit this one's.
                previous = (
                    getattr(agent, "stream_delta_callback", None),
                    getattr(agent, "tool_progress_callback", None),
                )
                if stream is not None:
                    agent.stream_delta_callback = stream.on_delta
                    agent.tool_progress_callback = stream.on_tool
                try:
                    agent.reset_interrupt()
                    # `in_conversation` is how a tool call — several frames deep
                    # inside the turn — finds its way back to this thread when it
                    # needs to ask a person something.
                    with in_conversation(key):
                        result = agent.run_conversation(user_message=text, conversation_history=history)
                finally:
                    state.agent = None
                    if stream is not None:
                        agent.stream_delta_callback, agent.tool_progress_callback = previous
                if state.epoch == epoch:
                    self._record_session(key, agent)
                return (result or {}).get("final_response", "") or ""

        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()

        if self._broker is not None:
            self._broker.register_asker(key, functools.partial(self._ask_approval, adapter, src))

        future = loop.run_in_executor(self._pool, ctx.run, _blocking)
        self._inflight.add(future)
        try:
            return await future
        finally:
            self._inflight.discard(future)
            if self._broker is not None:
                # Nothing may stay pending past the turn that raised it: a
                # question answered later would resume a turn that is over.
                self._broker.cancel_for(key, detail="the turn ended")
                self._broker.unregister_asker(key)

    # -- agents ---------------------------------------------------------------

    def resolve_settings(self, src: SessionSource) -> AgentSettings:
        """Toolset, model and iteration limit for a conversation.

        Precedence: a matching routing rule, then the platform's own block, then
        defaults — and the default toolset is the *restricted* one, so a platform
        nobody configured fails closed rather than getting a shell.
        """
        from gyrfalcon.config import cfg_get

        pcfg = self.config.platforms.get(src.platform)
        rule = self.config.route(src.route_candidates())

        # An elevated user gets the wider set — which is only defensible because
        # every dangerous call in it now stops and asks them first (D3).
        if pcfg is not None and pcfg.allow.is_elevated(src.user_id):
            toolset = pcfg.elevated_toolset or ELEVATED_DEFAULT_TOOLSET
        else:
            toolset = (rule and rule.toolset) or (pcfg and pcfg.toolset) or GATEWAY_DEFAULT_TOOLSET
        return AgentSettings(
            toolset=toolset,
            model=(rule and rule.model) or cfg_get("model.name", "") or "",
            max_iterations=(rule and rule.max_iterations) or (pcfg and pcfg.max_iterations) or DEFAULT_MAX_ITERATIONS,
        )

    def _create_agent(self, key: str, src: SessionSource) -> tuple[Any, Optional[list[dict]]]:
        """A new agent for `key`, resuming the thread's stored session if it has one.

        The idle cache evicts after an hour and the process restarts; without the
        stored mapping, coming back to a thread the next morning would silently
        start a new conversation.
        """
        session_id = self._session_db.get_meta(_SESSION_META + key) or None
        history = None
        if session_id:
            if self._session_db.get_session(session_id):
                history = self._session_db.get_messages_as_conversation(session_id)
            else:
                session_id = None
        return self._agent_factory(src, session_id, self.resolve_settings(src)), history

    def _record_session(self, key: str, agent: Any) -> None:
        session_id = getattr(agent, "session_id", None)
        if session_id and self._recorded.get(key) != session_id:
            self._session_db.set_meta(_SESSION_META + key, session_id)
            self._recorded[key] = session_id

    def _build_agent(self, src: SessionSource, session_id: Optional[str], settings: AgentSettings) -> Any:
        from gyrfalcon.run_agent import AIAgent

        base_url = api_key = None
        model = settings.model
        from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated

        if is_authenticated():
            base_url, api_key = get_copilot_credentials()
            if not model:
                model = "gpt-4o"

        return AIAgent(
            base_url=base_url,
            api_key=api_key,
            model=model,
            session_id=session_id,
            session_db=self._session_db,
            platform=src.platform,
            quiet_mode=True,
            max_iterations=settings.max_iterations,
            enabled_toolsets=[settings.toolset],
            # A chat platform's tool set is a security boundary: enforced at
            # dispatch and clamped through delegation and scheduling, not just
            # hidden from the model. See tools/restrictions.py.
            restrict_tools=True,
        )

    # -- approvals -------------------------------------------------------------

    async def _ask_approval(self, adapter: Optional[BasePlatformAdapter], src: SessionSource, request) -> bool:
        if adapter is None:
            return False
        try:
            return bool(await adapter.ask_approval(request, src))
        except Exception:
            logger.exception("Asking for approval failed")
            return False

    async def handle_decision(self, adapter: BasePlatformAdapter, decision: ApprovalInteraction) -> None:
        """Someone answered an approval question through a platform control.

        Authorised here, not in the adapter: a button posted in a channel can be
        clicked by anyone who can see it, including people the allowlist has
        never admitted. The same rule that decides who may talk to the agent
        decides who may let it run a dangerous command.
        """
        if self._broker is None:
            return
        pcfg = adapter.config
        chat_type = "dm" if decision.chat_id.startswith("D") else "channel"
        if not pcfg.allow.permits(decision.user_id, decision.chat_id, chat_type):
            logger.warning(
                f"Refused approval decision from unauthorised user={decision.user_id} "
                f"chat={decision.chat_id}"
            )
            return

        request = self._broker.resolve(decision.request_id, decision.approved, by=decision.user_id)
        if request is None:
            await adapter.settle_approval(decision.request_id, "_That request is no longer open._")
            return
        verdict = "Approved" if decision.approved else "Denied"
        await adapter.settle_approval(
            decision.request_id, f"*{verdict}* by <@{decision.user_id}> — {request.summary()}"
        )

    # -- control commands -----------------------------------------------------

    async def _handle_control(self, adapter: BasePlatformAdapter, event: MessageEvent, command: str, _arg: str) -> None:
        src = event.source
        key = src.session_key
        state = self._states.get(key)

        if command == "help":
            reply = _HELP
        elif command in ("approve", "deny"):
            reply = self._answer_pending(key, command == "approve", src.user_id)
        elif command == "stop":
            if state and (state.busy or state.pending):
                state.pending.clear()
                if self._broker is not None:
                    self._broker.cancel_for(key, detail="you stopped it")
                self._interrupt(state)
                reply = "Stopping."
            else:
                reply = "Nothing is running in this thread."
        elif command == "reset":
            state = state or self._states.setdefault(key, _SessionState())
            state.epoch += 1
            state.pending.clear()
            if self._broker is not None:
                self._broker.cancel_for(key, detail="the conversation was reset")
            self._interrupt(state)
            self._agent_cache.remove(key)
            self._recorded.pop(key, None)
            self._session_db.set_meta(_SESSION_META + key, "")
            reply = "Reset. The next message starts a fresh conversation."
        else:  # status
            settings = self.resolve_settings(src)
            busy = bool(state and state.busy)
            queued = len(state.pending) if state else 0
            reply = (
                f"{'Working' if busy else 'Idle'}"
                f"{f', {queued} message(s) queued' if queued else ''}. "
                f"Toolset: `{settings.toolset}`. Model: `{settings.model or 'default'}`."
            )
        await self._say(adapter, src, reply, reply_to=event)

    def _answer_pending(self, key: str, approved: bool, user_id: str) -> str:
        """`!approve` / `!deny` — the text form, for platforms without buttons
        and for anyone who would rather type."""
        if self._broker is None:
            return "Nothing is waiting for approval."
        request = self._broker.resolve_latest(key, approved, by=user_id)
        if request is None:
            return "Nothing is waiting for approval."
        return f"{'Approved' if approved else 'Denied'} — {request.summary()}"

    @staticmethod
    def _interrupt(state: _SessionState) -> None:
        agent = state.agent
        if agent is not None:
            try:
                agent.interrupt()
            except Exception:
                logger.exception("Interrupting agent failed")

    # -- outbound -------------------------------------------------------------

    async def _deliver(self, adapter: BasePlatformAdapter, event: MessageEvent, reply: str) -> None:
        await self._say(adapter, event.source, reply or "(The agent produced no reply.)", reply_to=event)

    async def _say(
        self, adapter: BasePlatformAdapter, src: SessionSource, text: str, reply_to: Optional[MessageEvent] = None,
    ) -> None:
        text = redact_secrets(text, adapter.secrets())
        anchor = src.thread_id or src.message_id or None
        try:
            result = await adapter.send(src.chat_id, text, reply_to=anchor, metadata={"source": src})
            if not result.success:
                logger.warning(f"Send to {src.platform}:{src.chat_id} failed: {result.error}")
        except Exception:
            logger.exception(f"Send to {src.platform}:{src.chat_id} raised")

    async def _ack(self, adapter: BasePlatformAdapter, event: MessageEvent, state: str) -> None:
        try:
            await adapter.acknowledge(event, state)
        except Exception:
            logger.debug("acknowledge failed", exc_info=True)
