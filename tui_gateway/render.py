"""Render helpers — format agent output for TUI display."""

from __future__ import annotations

import json
import textwrap
from typing import Any
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("render")



def render_markdown_to_ansi(text: str) -> str:
    """Basic markdown → ANSI rendering for terminal display."""
    logger.debug("Beginning of render_markdown_to_ansi")
    lines = text.split("\n")
    output = []

    in_code_block = False
    for line in lines:
        if line.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                output.append("\033[2m┌─────────────────────────────────────\033[0m")
            else:
                output.append("\033[2m└─────────────────────────────────────\033[0m")
            continue

        if in_code_block:
            output.append(f"\033[36m  {line}\033[0m")
            continue

        # Headers
        if line.startswith("### "):
            output.append(f"\033[1;33m{line[4:]}\033[0m")
        elif line.startswith("## "):
            output.append(f"\033[1;34m{line[3:]}\033[0m")
        elif line.startswith("# "):
            output.append(f"\033[1;35m{line[2:]}\033[0m")
        # Bold
        elif "**" in line:
            import re
            rendered = re.sub(r"\*\*(.+?)\*\*", r"\033[1m\1\033[0m", line)
            output.append(rendered)
        # Italic
        elif "*" in line and "**" not in line:
            import re
            rendered = re.sub(r"\*(.+?)\*", r"\033[3m\1\033[0m", line)
            output.append(rendered)
        # Inline code
        elif "`" in line:
            import re
            rendered = re.sub(r"`(.+?)`", r"\033[36m\1\033[0m", line)
            output.append(rendered)
        # Bullet points
        elif line.startswith("- ") or line.startswith("* "):
            output.append(f"  • {line[2:]}")
        else:
            output.append(line)

    return "\n".join(output)


def render_tool_call(name: str, args: dict[str, Any], emoji: str = "🔧") -> str:
    """Render a tool call for TUI display."""
    logger.debug("Beginning of render_tool_call")
    args_str = ", ".join(f"{k}={_truncate(v, 40)}" for k, v in args.items())
    return f"{emoji} \033[1m{name}\033[0m({args_str})"


def render_tool_result(name: str, result: Any, success: bool = True, emoji: str = "✓") -> str:
    """Render a tool result for TUI display."""
    logger.debug("Beginning of render_tool_result")
    status = "\033[32m✓\033[0m" if success else "\033[31m✗\033[0m"
    result_str = _truncate(str(result), 200)
    return f"  {status} {name}: {result_str}"


def render_token_usage(input_tokens: int, output_tokens: int, cached: int = 0) -> str:
    """Render token usage summary."""
    logger.debug("Beginning of render_token_usage")
    parts = [f"\033[2mTokens: {input_tokens} in"]
    if cached:
        parts.append(f"({cached} cached)")
    parts.append(f"→ {output_tokens} out")
    total = input_tokens + output_tokens
    parts.append(f"[{total} total]\033[0m")
    return " ".join(parts)


def render_thinking(text: str, max_lines: int = 3) -> str:
    """Render thinking/reasoning text (dimmed, truncated)."""
    logger.debug("Beginning of render_thinking")
    lines = text.strip().split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    rendered = "\n".join(f"  \033[2m💭 {line}\033[0m" for line in lines)
    return rendered


def render_error(message: str) -> str:
    """Render an error message."""
    logger.debug("Beginning of render_error")
    return f"\033[31m✗ Error:\033[0m {message}"


def render_status(message: str) -> str:
    """Render a status message."""
    logger.debug("Beginning of render_status")
    return f"\033[2m⟡ {message}\033[0m"


def render_approval_request(tool_name: str, args: dict[str, Any], reason: str) -> str:
    """Render an approval request for a dangerous operation."""
    logger.debug("Beginning of render_approval_request")
    lines = [
        "\033[33m⚠️  Approval Required\033[0m",
        f"  Tool: \033[1m{tool_name}\033[0m",
        f"  Reason: {reason}",
    ]
    if args:
        lines.append(f"  Args: {json.dumps(args, indent=2)[:200]}")
    lines.append("  \033[33m[Y]es / [N]o / [A]lways\033[0m")
    return "\n".join(lines)


def render_session_header(session_id: str, model: str, title: str = "") -> str:
    """Render session header bar."""
    logger.debug("Beginning of render_session_header")
    parts = [
        f"\033[1;34m━━━ Session: {session_id[:8]}\033[0m",
        f"\033[2m  Model: {model}\033[0m",
    ]
    if title:
        parts.append(f"\033[2m  Title: {title}\033[0m")
    return "\n".join(parts)


def wrap_text(text: str, width: int = 80) -> str:
    """Wrap text to terminal width."""
    logger.debug("Beginning of wrap_text")
    paragraphs = text.split("\n\n")
    wrapped = []
    for para in paragraphs:
        if para.startswith("  ") or para.startswith("```"):
            wrapped.append(para)  # Don't wrap code/indented blocks
        else:
            wrapped.append(textwrap.fill(para, width=width))
    return "\n\n".join(wrapped)


def _truncate(value: Any, max_len: int) -> str:
    """Truncate a value for display."""
    logger.debug("Beginning of _truncate")
    s = str(value)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s
