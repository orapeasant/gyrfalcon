"""Memory tools — explicit store, recall, and forget operations."""

from __future__ import annotations

import json
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.memory")


def _get_memory_manager(kwargs: dict):
    """Retrieve the MemoryManager from agent context."""
    logger.debug("Beginning of _get_memory_manager")
    agent_context = kwargs.get("agent_context")
    if agent_context and hasattr(agent_context, "memory_manager"):
        return agent_context.memory_manager

    # Fallback: try importing and instantiating directly
    try:
        from gyrfalcon.agent.memory_manager import MemoryManager
        return MemoryManager()
    except ImportError:
        return None


async def memory_store(args: dict, **kwargs) -> str:
    """Store a memory with a key and optional tags."""
    key = args.get("key", "")
    content = args.get("content", "")
    tags = args.get("tags", [])

    if not key:
        return json.dumps({"error": "No key provided"})
    if not content:
        return json.dumps({"error": "No content provided"})

    manager = _get_memory_manager(kwargs)
    if manager is None:
        return json.dumps({"error": "Memory manager not available"})

    try:
        # Use the manager's route_tool_call interface
        result = manager.route_tool_call("memory", {
            "action": "write",
            "key": key,
            "content": content,
        })

        # If tags provided, store them as metadata
        if tags:
            tag_content = f"[tags: {', '.join(tags)}]\n{content}"
            result = manager.route_tool_call("memory", {
                "action": "write",
                "key": key,
                "content": tag_content,
            })

        return json.dumps({
            "status": "stored",
            "key": key,
            "tags": tags,
            "content_length": len(content),
        })
    except Exception as e:
        logger.error(f"memory_store failed: {e}", exc_info=True)
        return json.dumps({"error": f"Store failed: {str(e)}"})


async def memory_recall(args: dict, **kwargs) -> str:
    """Recall memories matching a query."""
    query = args.get("query", "")
    limit = args.get("limit", 5)

    if not query:
        return json.dumps({"error": "No query provided"})

    manager = _get_memory_manager(kwargs)
    if manager is None:
        return json.dumps({"error": "Memory manager not available"})

    try:
        result = manager.route_tool_call("memory", {
            "action": "search",
            "query": query,
        })

        # Parse result and apply limit
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except (json.JSONDecodeError, TypeError):
            parsed = {"raw": result}

        # Apply limit to results if it's a list
        if isinstance(parsed, dict) and "results" in parsed:
            parsed["results"] = parsed["results"][:limit]

        return json.dumps({
            "query": query,
            "limit": limit,
            "memories": parsed,
        })
    except Exception as e:
        logger.error(f"memory_recall failed: {e}", exc_info=True)
        return json.dumps({"error": f"Recall failed: {str(e)}"})


async def memory_forget(args: dict, **kwargs) -> str:
    """Remove a memory by key."""
    key = args.get("key", "")

    if not key:
        return json.dumps({"error": "No key provided"})

    manager = _get_memory_manager(kwargs)
    if manager is None:
        return json.dumps({"error": "Memory manager not available"})

    try:
        # Write empty content to effectively clear the memory
        result = manager.route_tool_call("memory", {
            "action": "write",
            "key": key,
            "content": "",
        })

        return json.dumps({
            "status": "forgotten",
            "key": key,
        })
    except Exception as e:
        logger.error(f"memory_forget failed: {e}", exc_info=True)
        return json.dumps({"error": f"Forget failed: {str(e)}"})


# Register tools
registry.register(
    name="memory_store",
    toolset="memory",
    schema={
        "name": "memory_store",
        "description": "Store information in persistent memory with a key and optional tags for later retrieval.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Unique key to store the memory under"},
                "content": {"type": "string", "description": "Content to store"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization",
                    "default": [],
                },
            },
            "required": ["key", "content"],
        },
    },
    handler=memory_store,
    is_async=True,
    emoji="🧠",
)

registry.register(
    name="memory_recall",
    toolset="memory",
    schema={
        "name": "memory_recall",
        "description": "Recall memories matching a search query. Returns the most relevant stored memories.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to find relevant memories"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    handler=memory_recall,
    is_async=True,
    emoji="🔮",
)

registry.register(
    name="memory_forget",
    toolset="memory",
    schema={
        "name": "memory_forget",
        "description": "Remove a memory by its key. Use to clean up outdated or irrelevant information.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key of the memory to forget"},
            },
            "required": ["key"],
        },
    },
    handler=memory_forget,
    is_async=True,
    emoji="🗑️",
)
