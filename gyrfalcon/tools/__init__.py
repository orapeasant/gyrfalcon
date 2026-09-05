"""Central tool registry — self-registration at import time."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.registry")


@dataclass
class ToolEntry:
    name: str
    toolset: str
    schema: dict
    handler: Callable
    check_fn: Optional[Callable] = None
    requires_env: list[str] = field(default_factory=list)
    is_async: bool = False
    emoji: str = "🔧"
    max_result_size_chars: int = 50_000
    read_only: bool = False   # True = never requires approval; False = may require approval


class ToolRegistry:
    """Singleton registry collecting schemas and handlers from all tool files."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._toolset_aliases: dict[str, str] = {}
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        emoji: str = "🔧",
        max_result_size_chars: int = 50_000,
        read_only: bool = False,
    ) -> None:
        entry = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env or [],
            is_async=is_async,
            emoji=emoji,
            max_result_size_chars=max_result_size_chars,
            read_only=read_only,
        )
        with self._lock:
            self._tools[name] = entry
            self._generation += 1
        logger.debug(f"Registered tool: {name} (toolset={toolset})")

    def deregister(self, name: str) -> None:
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                self._generation += 1

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        return self._tools.get(name)

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Execute tool handler. Returns JSON string."""
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})

        try:
            result = entry.handler(args, **kwargs)
            if not isinstance(result, str):
                result = json.dumps(result)
            if len(result) > entry.max_result_size_chars:
                result = result[: entry.max_result_size_chars] + "\n... [truncated]"
            return result
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}", exc_info=True)
            return json.dumps({"error": str(e)})

    def get_schemas(
        self, tool_names: list[str] | None = None, check_availability: bool = True
    ) -> list[dict]:
        """Get OpenAI-format tool schemas."""
        entries = self._tools.values() if tool_names is None else [
            self._tools[n] for n in tool_names if n in self._tools
        ]
        schemas = []
        for entry in entries:
            if check_availability and entry.check_fn and not entry.check_fn():
                continue
            if check_availability and entry.requires_env:
                import os
                if not all(os.environ.get(var) for var in entry.requires_env):
                    continue
            schemas.append({
                "type": "function",
                "function": entry.schema,
            })
        return schemas

    def get_tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def get_tool_names_for_toolset(self, toolset: str) -> list[str]:
        resolved = self._toolset_aliases.get(toolset, toolset)
        return [name for name, entry in self._tools.items() if entry.toolset == resolved]

    def register_toolset_alias(self, alias: str, toolset: str) -> None:
        self._toolset_aliases[alias] = toolset

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    def is_read_only(self, name: str) -> bool:
        """Return True if the tool is flagged as read-only (no approval needed)."""
        entry = self._tools.get(name)
        return entry.read_only if entry else False

    def snapshot(self) -> dict[str, ToolEntry]:
        """Thread-safe snapshot for concurrent readers."""
        with self._lock:
            return dict(self._tools)


# Global singleton
registry = ToolRegistry()
