"""Todo tool — in-memory task tracking."""

from __future__ import annotations

import json
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.todo")

# Global todo store (per-session, reset on session change)
_todos: list[dict] = []


class TodoStore:
    """In-memory task list. Survives context compression."""

    def __init__(self):
        self._items: list[dict] = []
        self._next_id = 1

    def write(self, todos: list[dict], merge: bool = False) -> list[dict]:
        """Write/replace todo list."""
        logger.debug("Beginning of write")
        if not merge:
            self._items = []
            self._next_id = 1

        for todo in todos:
            item = {
                "id": self._next_id,
                "title": todo.get("title", ""),
                "status": todo.get("status", "pending"),
                "details": todo.get("details", ""),
            }
            self._items.append(item)
            self._next_id += 1

        return self._items

    def read(self) -> list[dict]:
        logger.debug("Beginning of read")
        return self._items

    def update(self, todo_id: int, updates: dict) -> Optional[dict]:
        logger.debug("Beginning of update")
        for item in self._items:
            if item["id"] == todo_id:
                item.update(updates)
                return item
        return None

    def format_for_injection(self) -> Optional[str]:
        """Format todos for system prompt injection after compression."""
        logger.debug("Beginning of format_for_injection")
        if not self._items:
            return None
        lines = ["## Active Tasks"]
        for item in self._items:
            status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅", "cancelled": "❌"}.get(
                item["status"], "⬜"
            )
            lines.append(f"{status_icon} [{item['id']}] {item['title']}")
        return "\n".join(lines)


_store = TodoStore()


def todo_tool(args: dict, **kwargs) -> str:
    """Manage task list."""
    logger.debug("Beginning of todo_tool")
    action = args.get("action", "read")

    if action == "read":
        items = _store.read()
        return json.dumps({"todos": items})

    elif action == "write":
        todos = args.get("todos", [])
        merge = args.get("merge", False)
        items = _store.write(todos, merge=merge)
        return json.dumps({"status": "updated", "todos": items})

    elif action == "update":
        todo_id = args.get("id")
        status = args.get("status")
        if todo_id is None:
            return json.dumps({"error": "id is required"})
        updates = {}
        if status:
            updates["status"] = status
        if args.get("title"):
            updates["title"] = args["title"]
        result = _store.update(int(todo_id), updates)
        if result:
            return json.dumps({"status": "updated", "todo": result})
        return json.dumps({"error": f"Todo {todo_id} not found"})

    return json.dumps({"error": f"Unknown action: {action}"})


# Register
registry.register(
    name="todo",
    toolset="todo",
    schema={
        "name": "todo",
        "description": "Manage a task list. Actions: 'read' (list all), 'write' (create/replace tasks), 'update' (update status of a task).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "write", "update"]},
                "todos": {"type": "array", "items": {"type": "object"}, "description": "Tasks to create (for 'write')"},
                "merge": {"type": "boolean", "description": "Merge with existing (for 'write')", "default": False},
                "id": {"type": "integer", "description": "Task ID (for 'update')"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                "title": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    handler=todo_tool,
    emoji="📋",
)
