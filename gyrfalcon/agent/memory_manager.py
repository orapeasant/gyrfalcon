"""Memory orchestration — manages active memory provider."""

from __future__ import annotations

from typing import Optional

from gyrfalcon.agent.memory_provider import MemoryProvider, BuiltinMemoryProvider
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("agent.memory_manager")


class MemoryManager:
    """Orchestrates memory providers. Only ONE external provider active at a time."""

    def __init__(self):
        self._builtin = BuiltinMemoryProvider()
        self._external: Optional[MemoryProvider] = None
        self._tool_routing: dict[str, MemoryProvider] = {}

    def add_provider(self, provider: MemoryProvider) -> None:
        """Set the external memory provider (max 1)."""
        logger.debug("Beginning of add_provider")
        if self._external:
            logger.warning(f"Replacing memory provider {self._external.name} with {provider.name}")
        self._external = provider

    def initialize(self, session_id: str, **kwargs) -> None:
        logger.debug("Beginning of initialize")
        self._builtin.initialize(session_id, **kwargs)
        if self._external and self._external.is_available():
            self._external.initialize(session_id, **kwargs)
        self._build_routing()

    def _build_routing(self) -> None:
        """Build tool name → provider routing index."""
        logger.debug("Beginning of _build_routing")
        self._tool_routing.clear()
        for schema in self._builtin.get_tool_schemas():
            self._tool_routing[schema["name"]] = self._builtin
        if self._external and self._external.is_available():
            for schema in self._external.get_tool_schemas():
                self._tool_routing[schema["name"]] = self._external

    def build_system_prompt(self) -> str:
        logger.debug("Beginning of build_system_prompt")
        parts = []
        builtin_prompt = self._builtin.system_prompt_block()
        if builtin_prompt:
            parts.append(builtin_prompt)
        if self._external and self._external.is_available():
            ext_prompt = self._external.system_prompt_block()
            if ext_prompt:
                parts.append(ext_prompt)
        return "\n\n".join(parts)

    def prefetch_all(self, user_message: str) -> str:
        logger.debug("Beginning of prefetch_all")
        parts = []
        builtin_pf = self._builtin.prefetch(user_message)
        if builtin_pf:
            parts.append(builtin_pf)
        if self._external and self._external.is_available():
            ext_pf = self._external.prefetch(user_message)
            if ext_pf:
                parts.append(ext_pf)
        return "\n".join(parts)

    def sync_all(self, user_msg: str, assistant_response: str) -> None:
        logger.debug("Beginning of sync_all")
        self._builtin.sync_turn(user_msg, assistant_response)
        if self._external and self._external.is_available():
            self._external.sync_turn(user_msg, assistant_response)

    def get_tool_schemas(self) -> list[dict]:
        logger.debug("Beginning of get_tool_schemas")
        schemas = list(self._builtin.get_tool_schemas())
        if self._external and self._external.is_available():
            schemas.extend(self._external.get_tool_schemas())
        return schemas

    def route_tool_call(self, tool_name: str, args: dict) -> str:
        logger.debug("Beginning of route_tool_call")
        provider = self._tool_routing.get(tool_name)
        if provider:
            return provider.handle_tool_call(tool_name, args)
        return '{"error": "No memory provider for tool"}'

    def shutdown(self) -> None:
        logger.debug("Beginning of shutdown")
        self._builtin.shutdown()
        if self._external:
            self._external.shutdown()
