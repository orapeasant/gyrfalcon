"""Fakes for the gateway: an adapter, an agent and a session store that need no
network, no LLM and no real profile directory. Imported by name — this tree has no packages."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional

from gyrfalcon.gateway.config import GatewayConfig, PlatformConfig
from gyrfalcon.gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult, SessionSource
from gyrfalcon.gateway.run import GatewayRunner
from gyrfalcon.identity import get_principal


def run(coro, timeout: float = 20.0):
    """Run a coroutine to completion. No pytest-asyncio dependency.

    Bounded: a regression that deadlocks the code under test (say, awaiting an
    agent inside a socket listener) must fail the test, not hang the suite."""
    async def bounded():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(bounded())


class FakeAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig, secret_values: Optional[list[str]] = None):
        super().__init__(config)
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.settled: dict[str, str] = {}
        self.acks: list[tuple[str, str]] = []
        self.connected = False
        self.disconnected = False
        self._secrets = secret_values or []

    platform_name = "fake"

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to})
        return SendResult(success=True, message_id=str(len(self.sent)))

    async def edit_message(self, chat_id, message_id, content) -> bool:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return True

    async def settle_approval(self, request_id, text) -> None:
        self.settled[request_id] = text

    async def get_chat_info(self, chat_id) -> dict:
        return {"id": chat_id}

    async def acknowledge(self, event: MessageEvent, state: str) -> None:
        self.acks.append((event.text, state))

    def secrets(self) -> list[str]:
        return self._secrets

    @property
    def texts(self) -> list[str]:
        return [m["content"] for m in self.sent]


class FakeAgent:
    """Records where and as whom it ran; can block until released."""

    active = 0
    max_active = 0
    lock = threading.Lock()

    def __init__(self, reply: str = "ok", block: bool = False, raises: Optional[Exception] = None,
                 session_id: Optional[str] = None, probe: Any = None):
        self.reply = reply
        self.raises = raises
        self.session_id = session_id
        self.probe = probe
        self.calls: list[dict] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.interrupted = False
        self.reset_calls = 0

    def reset_interrupt(self) -> None:
        self.reset_calls += 1

    def interrupt(self) -> None:
        self.interrupted = True
        self.release.set()

    def run_conversation(self, user_message, conversation_history=None, **_):
        with FakeAgent.lock:
            FakeAgent.active += 1
            FakeAgent.max_active = max(FakeAgent.max_active, FakeAgent.active)
        try:
            self.calls.append({
                "message": user_message,
                "history": conversation_history,
                "thread": threading.current_thread().name,
                "principal": get_principal(),
                "probe": self.probe.get() if self.probe is not None else None,
            })
            self.started.set()
            assert self.release.wait(5), "test never released the agent"
            if self.raises:
                raise self.raises
            if not self.session_id:
                self.session_id = f"sess-{id(self)}"
            return {"final_response": self.reply}
        finally:
            with FakeAgent.lock:
                FakeAgent.active -= 1


class FakeSessionDB:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.sessions: dict[str, list[dict]] = {}
        self.closed = False

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_session(self, session_id):
        return {"id": session_id} if session_id in self.sessions else None

    def get_messages_as_conversation(self, session_id):
        return list(self.sessions[session_id])

    def close(self):
        self.closed = True


def make_source(user="U1", chat="D1", thread="t1", chat_type="dm", **kw) -> SessionSource:
    return SessionSource(platform="fake", chat_id=chat, user_id=user, thread_id=thread, chat_type=chat_type, **kw)


def ev(text="hello", event_id="", **kw) -> MessageEvent:
    return MessageEvent(source=make_source(**kw), text=text, event_id=event_id)


DEFAULT_PLATFORM = {"allow": {"users": ["U1"], "channels": ["C1"]}}


class Harness:
    """A runner wired to fakes, plus the agents it built."""

    def __init__(self, platform: Optional[dict] = None, gateway: Optional[dict] = None,
                 agent_factory: Optional[Callable] = None, secret_values: Optional[list[str]] = None):
        raw = dict(gateway or {})
        raw["platforms"] = {"fake": DEFAULT_PLATFORM if platform is None else platform}
        self.config = GatewayConfig.from_dict(raw)
        self.db = FakeSessionDB()
        self.agents: list[FakeAgent] = []
        self.factory_calls: list[tuple] = []
        self._factory = agent_factory or (lambda src, sid, settings: FakeAgent())
        self.runner = GatewayRunner(
            self.config, agent_factory=self._build, session_db=self.db, run_scheduler=False,
            adapter_loader=lambda name: (lambda cfg: FakeAdapter(cfg, secret_values)),
        )
        self.adapter = FakeAdapter(self.config.platforms["fake"], secret_values)
        self.adapter.on_message(lambda e: self.runner.handle_event(self.adapter, e))

    def _build(self, src, session_id, settings):
        self.factory_calls.append((src, session_id, settings))
        agent = self._factory(src, session_id, settings)
        self.agents.append(agent)
        return agent

    async def send(self, event: MessageEvent) -> None:
        await self.adapter.dispatch(event)


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll from the event loop — which must stay free to run this at all."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)
