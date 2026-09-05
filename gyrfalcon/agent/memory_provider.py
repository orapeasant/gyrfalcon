"""Memory provider ABC and built-in memory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("memory_provider")



class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        logger.debug("Beginning of is_available")
        ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        logger.debug("Beginning of initialize")
        ...

    @abstractmethod
    def get_tool_schemas(self) -> list[dict]:
        logger.debug("Beginning of get_tool_schemas")
        ...

    def system_prompt_block(self) -> str:
        logger.debug("Beginning of system_prompt_block")
        return ""

    def prefetch(self, query: str) -> str:
        logger.debug("Beginning of prefetch")
        return ""

    def sync_turn(self, user_msg: str, assistant_msg: str) -> None:
        logger.debug("Beginning of sync_turn")
        pass

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        logger.debug("Beginning of handle_tool_call")
        return ""

    def shutdown(self) -> None:
        logger.debug("Beginning of shutdown")
        pass

    # Optional hooks
    def on_turn_start(self) -> None:
        logger.debug("Beginning of on_turn_start")
        pass

    def on_session_end(self) -> None:
        logger.debug("Beginning of on_session_end")
        pass

    def on_pre_compress(self) -> None:
        logger.debug("Beginning of on_pre_compress")
        pass

    def on_memory_write(self) -> None:
        logger.debug("Beginning of on_memory_write")
        pass

    def on_delegation(self) -> None:
        logger.debug("Beginning of on_delegation")
        pass


class BuiltinMemoryProvider(MemoryProvider):
    """Built-in file-based memory (MEMORY.md, USER.md)."""

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        logger.debug("Beginning of is_available")
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        logger.debug("Beginning of initialize")
        self._home = get_gyrfalcon_home()

    def get_tool_schemas(self) -> list[dict]:
        logger.debug("Beginning of get_tool_schemas")
        return [{
            "name": "memory",
            "description": "Store or retrieve information from persistent memory. Actions: 'read' (read memory), 'write' (write/append to memory), 'search' (search memory).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "search"],
                        "description": "The memory action to perform",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key/category (e.g., 'user_preferences', 'project_context')",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write (for 'write' action)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for 'search' action)",
                    },
                },
                "required": ["action"],
            },
        }]

    def system_prompt_block(self) -> str:
        """Load memory context for system prompt."""
        logger.debug("Beginning of system_prompt_block")
        parts = []
        memory_file = self._home / "MEMORY.md"
        if memory_file.exists():
            try:
                content = memory_file.read_text(errors="replace")[:5000]
                parts.append(f"Your persistent memory:\n{content}")
            except OSError:
                pass

        user_file = self._home / "USER.md"
        if user_file.exists():
            try:
                content = user_file.read_text(errors="replace")[:3000]
                parts.append(f"User information:\n{content}")
            except OSError:
                pass

        return "\n\n".join(parts)

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        """Handle memory tool calls."""
        logger.debug("Beginning of handle_tool_call")
        import json

        action = args.get("action", "read")
        key = args.get("key", "general")

        if action == "read":
            return self._read_memory(key)
        elif action == "write":
            content = args.get("content", "")
            return self._write_memory(key, content)
        elif action == "search":
            query = args.get("query", "")
            return self._search_memory(query)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})

    def _read_memory(self, key: str) -> str:
        logger.debug("Beginning of _read_memory")
        import json
        memory_file = self._home / "MEMORY.md"
        if memory_file.exists():
            content = memory_file.read_text(errors="replace")
            return json.dumps({"content": content})
        return json.dumps({"content": ""})

    def _write_memory(self, key: str, content: str) -> str:
        logger.debug("Beginning of _write_memory")
        import json
        memory_file = self._home / "MEMORY.md"
        existing = ""
        if memory_file.exists():
            existing = memory_file.read_text(errors="replace")

        # Append under key heading
        section = f"\n\n## {key}\n\n{content}\n"
        memory_file.write_text(existing + section)
        return json.dumps({"status": "written", "key": key})

    def _search_memory(self, query: str) -> str:
        logger.debug("Beginning of _search_memory")
        import json
        memory_file = self._home / "MEMORY.md"
        if not memory_file.exists():
            return json.dumps({"results": []})

        content = memory_file.read_text(errors="replace")
        lines = content.split("\n")
        matches = [line for line in lines if query.lower() in line.lower()]
        return json.dumps({"results": matches[:20]})
