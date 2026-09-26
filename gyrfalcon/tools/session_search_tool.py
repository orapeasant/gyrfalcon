"""Session search tool — search past sessions via FTS5."""

from __future__ import annotations

import json
import time

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.session_search")


def session_search(args: dict, **kwargs) -> str:
    """Search past sessions via FTS5 or list recent messages."""
    logger.debug("Beginning of session_search")
    query = args.get("query", "")
    role_filter = args.get("role_filter")
    limit = args.get("limit", 5)
    action = args.get("action", "search")

    from gyrfalcon.gyrfalcon_state import SessionDB
    db = SessionDB()

    try:
        if action == "list_recent":
            # List recent messages chronologically
            rows = db.list_recent_messages(role_filter or "user", limit)
            formatted = []
            for row in rows:
                formatted.append({
                    "session_id": row["session_id"][:8],
                    "session_title": row["session_title"] or "",
                    "role": row["role"],
                    "content_preview": (row["content"] or "")[:500],
                    "timestamp": row["created_at"],
                })
            return json.dumps({"results": formatted, "action": "list_recent"})

        # FTS search
        if not query:
            return json.dumps({"error": "No query provided"})

        results = db.search_messages(query, limit=limit * 3)

        if role_filter:
            results = [r for r in results if r.get("role") == role_filter]

        results = results[:limit]

        formatted = []
        for r in results:
            formatted.append({
                "session_id": r.get("session_id", "")[:8],
                "session_title": r.get("session_title", ""),
                "role": r.get("role", ""),
                "content_preview": (r.get("content", "") or "")[:500],
                "timestamp": r.get("created_at"),
            })

        return json.dumps({"results": formatted, "query": query})
    except Exception as e:
        return json.dumps({"error": f"Search failed: {str(e)}"})
    finally:
        db.close()


# Register
registry.register(
    name="session_search",
    toolset="session_search",
    schema={
        "name": "session_search",
        "description": "Search the user's past conversation history stored in the local database. Use this when the user asks about previous questions, past discussions, or what they talked about before (e.g. 'what did I ask last week', 'what was my first question'). Use action='list_recent' to see chronological history.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "list_recent"],
                    "description": "Action: 'search' for FTS query, 'list_recent' to list recent messages chronologically",
                    "default": "search",
                },
                "query": {"type": "string", "description": "Search query to match against past messages. Use broad terms for better recall. Required for 'search' action."},
                "role_filter": {"type": "string", "enum": ["user", "assistant", "tool"], "description": "Filter results by message role"},
                "limit": {"type": "integer", "description": "Max results to return", "default": 5},
            },
            "required": ["action"],
        },
    },
    handler=session_search,
    emoji="🔎",
    read_only=True,
)
