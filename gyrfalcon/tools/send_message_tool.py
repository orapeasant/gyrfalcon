"""Send message tool — push messages to users in gateway/async contexts."""

from __future__ import annotations

import json
import uuid
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.send_message")


async def send_message(args: dict, **kwargs) -> str:
    """Send a message to the user via the gateway."""
    text = args.get("text", "")
    channel = args.get("channel")
    reply_to = args.get("reply_to")
    format_type = args.get("format", "text")

    if not text:
        return json.dumps({"error": "No text provided"})

    message_id = str(uuid.uuid4())

    # Try to use the gateway from agent context
    agent_context = kwargs.get("agent_context")
    gateway = None

    if agent_context:
        gateway = getattr(agent_context, "gateway", None) or getattr(
            agent_context, "message_gateway", None
        )

    if gateway is not None:
        try:
            result = await gateway.send_message(
                text=text,
                channel=channel,
                reply_to=reply_to,
                format=format_type,
                message_id=message_id,
            )
            return json.dumps({
                "status": "sent",
                "message_id": message_id,
                "channel": channel,
                "reply_to": reply_to,
                "format": format_type,
                **(result if isinstance(result, dict) else {}),
            })
        except Exception as e:
            logger.error(f"Gateway send failed: {e}", exc_info=True)
            return json.dumps({"error": f"Send failed: {str(e)}", "message_id": message_id})

    # Fallback: log the message and return success (useful for testing/local mode)
    logger.info(f"[send_message] channel={channel} reply_to={reply_to} format={format_type}: {text[:100]}")

    return json.dumps({
        "status": "sent",
        "message_id": message_id,
        "channel": channel,
        "reply_to": reply_to,
        "format": format_type,
        "note": "No gateway configured — message logged locally",
    })


# Register tool
registry.register(
    name="send_message",
    toolset="gateway",
    schema={
        "name": "send_message",
        "description": "Send a message to the user or a specific channel. Used in gateway/async contexts to push messages to platforms.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send"},
                "channel": {
                    "type": "string",
                    "description": "Target channel or conversation ID (optional, uses default if not specified)",
                },
                "reply_to": {
                    "type": "string",
                    "description": "Message ID to reply to (for threaded conversations)",
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "markdown", "html"],
                    "default": "text",
                    "description": "Message format",
                },
            },
            "required": ["text"],
        },
    },
    handler=send_message,
    is_async=True,
    emoji="📨",
)
