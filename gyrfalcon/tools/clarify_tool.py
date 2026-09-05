"""Clarify tool — request clarification from the user."""

from __future__ import annotations

import json

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.clarify")


def clarify_tool(args: dict, **kwargs) -> str:
    """Request clarification from the user."""
    logger.debug("Beginning of clarify_tool")
    question = args.get("question", "")
    options = args.get("options", [])

    if not question:
        return json.dumps({"error": "No question provided"})

    # In CLI mode, this would trigger a callback to the UI
    # The clarify_callback on AIAgent handles the actual interaction
    return json.dumps({
        "clarification_needed": True,
        "question": question,
        "options": options,
    })


# Register
registry.register(
    name="clarify",
    toolset="core",
    schema={
        "name": "clarify",
        "description": "Ask the user a clarifying question when the task is ambiguous.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of choices",
                },
            },
            "required": ["question"],
        },
    },
    handler=clarify_tool,
    emoji="❓",
)
