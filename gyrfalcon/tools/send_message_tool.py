"""Send a message to a chat platform, unprompted.

For work that finishes when nobody is looking at the terminal: a long task
started from the CLI, a flow reporting a result. Ordinary replies do not go
through here — the gateway already sends those back the way they came.

This used to call `agent_context.gateway.send_message(...)`, an interface no
object in the codebase implemented, so it always fell through to a branch that
logged locally and reported `"status": "sent"` — a success the caller had no
reason to doubt and no message to show for. It now goes through the delivery
router (`gateway/delivery.py`) and reports honestly when there is nothing to
deliver through.
"""

from __future__ import annotations

import json

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.tools import registry

logger = get_logger("tools.send_message")


def send_message(args: dict, **kwargs) -> str:
    """Send a message to a platform destination such as `slack:C0123`."""
    text = (args.get("text") or "").strip()
    target = (args.get("channel") or "").strip()

    if not text:
        return json.dumps({"error": "No text provided"})
    if not target:
        return json.dumps({
            "error": "No channel provided. Give a destination like 'slack:C0123' "
                     "(the platform, then the channel or user id)."
        })

    from gyrfalcon.gateway.delivery import deliver_from_anywhere, get_router, parse_target

    if parse_target(target) is None:
        return json.dumps({
            "error": f"{target!r} is not a destination. Use '<platform>:<channel-or-user-id>', "
                     "for example 'slack:C0123'."
        })

    result = deliver_from_anywhere(target, text)
    if result.ok:
        return json.dumps({"status": "sent", "channel": target, "message_id": result.detail})

    router = get_router()
    available = router.describe_targets() if router else []
    return json.dumps({
        "status": "failed",
        "channel": target,
        "error": result.detail,
        "connected_platforms": available,
    })


registry.register(
    name="send_message",
    toolset="gateway",
    schema={
        "name": "send_message",
        "description": (
            "Send a message to a chat platform destination, such as a Slack channel. "
            "Use this to report the result of work the user is not watching — a long task, "
            "or a scheduled job. Replies to a message you are already answering are sent "
            "automatically and do not need this tool. Requires the gateway to be running."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send"},
                "channel": {
                    "type": "string",
                    "description": "Destination as '<platform>:<id>', e.g. 'slack:C0123' or 'slack:@U0456'",
                },
            },
            "required": ["text", "channel"],
        },
    },
    handler=send_message,
    emoji="📨",
)
