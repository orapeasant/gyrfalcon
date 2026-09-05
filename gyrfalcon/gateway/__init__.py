"""Gateway — multi-platform message routing framework."""

from __future__ import annotations

import asyncio
import time
import threading
from collections import OrderedDict
from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger, set_session_tag
from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.config import load_config, cfg_get
from gyrfalcon.run_agent import AIAgent
from gyrfalcon.scheduler import scheduler

logger = get_logger("gateway")


class AgentCache:
    """LRU cache for agent sessions."""

    def __init__(self, max_size: int = 128, ttl: int = 3600):
        self._cache: OrderedDict[str, tuple[AIAgent, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, session_key: str) -> Optional[AIAgent]:
        with self._lock:
            if session_key in self._cache:
                agent, ts = self._cache[session_key]
                if time.time() - ts < self._ttl:
                    self._cache.move_to_end(session_key)
                    return agent
                else:
                    del self._cache[session_key]
            return None

    def put(self, session_key: str, agent: AIAgent) -> None:
        with self._lock:
            self._cache[session_key] = (agent, time.time())
            self._cache.move_to_end(session_key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def remove(self, session_key: str) -> None:
        with self._lock:
            self._cache.pop(session_key, None)

    def size(self) -> int:
        return len(self._cache)


class GatewayConfig:
    """Gateway configuration."""

    def __init__(self):
        config = load_config()
        gw_config = config.get("gateway", {})
        self.platforms = gw_config.get("platforms", {})
        self.agent_cache_size = gw_config.get("agent_cache_size", 128)
        self.idle_ttl = gw_config.get("idle_ttl", 3600)


class GatewayRunner:
    """Main gateway controller managing platform adapter lifecycles and message routing."""

    def __init__(self, config: GatewayConfig | None = None):
        self.config = config or GatewayConfig()
        self._agent_cache = AgentCache(
            max_size=self.config.agent_cache_size,
            ttl=self.config.idle_ttl,
        )
        self._session_db = SessionDB()
        self._adapters: dict[str, Any] = {}
        self._running = False

    async def start(self) -> None:
        """Connect all platforms, start scheduler, begin message processing."""
        self._running = True
        logger.info("Gateway starting...")

        # Start scheduler
        scheduler.start()

        # Load and connect platform adapters
        for platform_name, platform_config in self.config.platforms.items():
            if not platform_config.get("enabled", True):
                continue
            try:
                adapter = self._create_adapter(platform_name, platform_config)
                if adapter:
                    connected = await adapter.connect()
                    if connected:
                        self._adapters[platform_name] = adapter
                        logger.info(f"Connected platform: {platform_name}")
                    else:
                        logger.warning(f"Failed to connect: {platform_name}")
            except Exception as e:
                logger.error(f"Platform {platform_name} error: {e}")

        logger.info(f"Gateway running with {len(self._adapters)} platform(s)")

        # Keep running
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Graceful shutdown with drain period."""
        self._running = False
        logger.info("Gateway stopping...")

        # Disconnect adapters
        for name, adapter in self._adapters.items():
            try:
                await adapter.disconnect()
                logger.info(f"Disconnected: {name}")
            except Exception as e:
                logger.error(f"Error disconnecting {name}: {e}")

        # Stop scheduler
        scheduler.stop()

        self._session_db.close()
        logger.info("Gateway stopped")

    def _create_adapter(self, platform_name: str, config: dict) -> Optional[Any]:
        """Create platform adapter instance."""
        # Platform adapters are loaded dynamically
        # For now, return None as we're not implementing specific platforms
        logger.info(f"Platform adapter for '{platform_name}' not yet implemented")
        return None

    async def handle_message(
        self, platform: str, chat_id: str, user_id: str,
        message: str, thread_id: str = "", metadata: dict | None = None
    ) -> str:
        """Route an incoming message to the appropriate agent."""
        session_key = f"{platform}:{chat_id}:{thread_id}"

        # Get or create agent
        agent = self._agent_cache.get(session_key)
        if not agent:
            agent = self._create_agent_for_session(session_key, platform)
            self._agent_cache.put(session_key, agent)

        # Set session tag for logging context in this thread
        if agent.session_id:
            set_session_tag(f"[{agent.session_id[:8]}] ")

        # Process message
        try:
            result = agent.run_conversation(user_message=message)
            return result.get("final_response", "")
        except Exception as e:
            logger.error(f"Agent error for {session_key}: {e}")
            return f"Error: {str(e)}"

    def _create_agent_for_session(self, session_key: str, platform: str) -> AIAgent:
        """Create a new agent for a gateway session."""
        config = load_config()
        model = config.get("model", {}).get("name", "")

        # Resolve credentials
        base_url = None
        api_key = None
        from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated
        if is_authenticated():
            base_url, api_key = get_copilot_credentials()
            if not model:
                model = "gpt-4o"

        return AIAgent(
            base_url=base_url,
            api_key=api_key,
            model=model,
            session_db=self._session_db,
            platform=platform,
            quiet_mode=True,
        )
