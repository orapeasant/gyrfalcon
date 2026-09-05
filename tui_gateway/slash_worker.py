"""Slash worker — executes slash commands asynchronously for TUI/CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gyrfalcon.config import cfg_get, cfg_set
from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("slash_worker")



@dataclass
class SlashResult:
    """Result of a slash command execution."""
    success: bool
    output: str
    data: dict[str, Any] | None = None


# Command definitions
CommandHandler = Callable[..., SlashResult]

_commands: dict[str, "CommandDef"] = {}


@dataclass
class CommandDef:
    """Definition of a slash command."""
    name: str
    aliases: list[str]
    description: str
    handler: CommandHandler
    category: str = "general"
    hidden: bool = False


def register_command(
    name: str,
    handler: CommandHandler,
    description: str = "",
    aliases: list[str] | None = None,
    category: str = "general",
    hidden: bool = False,
) -> None:
    """Register a slash command."""
    logger.debug("Beginning of register_command")
    cmd = CommandDef(
        name=name,
        aliases=aliases or [],
        description=description,
        handler=handler,
        category=category,
        hidden=hidden,
    )
    _commands[name] = cmd
    for alias in cmd.aliases:
        _commands[alias] = cmd


def get_command(name: str) -> Optional[CommandDef]:
    """Look up a command by name or alias."""
    logger.debug("Beginning of get_command")
    return _commands.get(name)


def list_commands(include_hidden: bool = False) -> list[CommandDef]:
    """List all registered commands (deduplicated)."""
    logger.debug("Beginning of list_commands")
    seen = set()
    result = []
    for cmd in _commands.values():
        if cmd.name not in seen:
            if include_hidden or not cmd.hidden:
                result.append(cmd)
                seen.add(cmd.name)
    return sorted(result, key=lambda c: c.name)


# --- Built-in commands ---

def _cmd_help(**kwargs: Any) -> SlashResult:
    """Show available commands."""
    logger.debug("Beginning of _cmd_help")
    commands = list_commands()
    categories: dict[str, list[CommandDef]] = {}
    for cmd in commands:
        categories.setdefault(cmd.category, []).append(cmd)

    lines = ["Available commands:\n"]
    for cat, cmds in sorted(categories.items()):
        lines.append(f"  [{cat}]")
        for cmd in cmds:
            alias_str = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
            lines.append(f"    /{cmd.name}{alias_str} — {cmd.description}")
        lines.append("")

    return SlashResult(success=True, output="\n".join(lines))


def _cmd_clear(session_db: SessionDB | None = None, session_id: str | None = None, **kwargs: Any) -> SlashResult:
    """Clear current session context."""
    logger.debug("Beginning of _cmd_clear")
    return SlashResult(success=True, output="Session context cleared.", data={"action": "clear"})


def _cmd_model(args: str = "", **kwargs: Any) -> SlashResult:
    """Switch or show current model."""
    logger.debug("Beginning of _cmd_model")
    if not args:
        current = cfg_get("model.default", "gpt-4o")
        return SlashResult(success=True, output=f"Current model: {current}")
    cfg_set("model.default", args.strip())
    return SlashResult(success=True, output=f"Model switched to: {args.strip()}")


def _cmd_compact(**kwargs: Any) -> SlashResult:
    """Compress conversation context."""
    logger.debug("Beginning of _cmd_compact")
    return SlashResult(success=True, output="Context compacted.", data={"action": "compact"})


def _cmd_status(session_db: SessionDB | None = None, **kwargs: Any) -> SlashResult:
    """Show agent status."""
    logger.debug("Beginning of _cmd_status")
    lines = [
        "Agent Status:",
        f"  Model: {cfg_get('model.default', 'gpt-4o')}",
        f"  Provider: {cfg_get('provider.active', 'copilot')}",
        f"  Tools: enabled",
    ]
    return SlashResult(success=True, output="\n".join(lines))


def _cmd_config(args: str = "", **kwargs: Any) -> SlashResult:
    """View or set config values."""
    logger.debug("Beginning of _cmd_config")
    if not args:
        return SlashResult(success=True, output="Usage: /config <key> [value]")
    parts = args.split(None, 1)
    key = parts[0]
    if len(parts) == 1:
        val = cfg_get(key, "<not set>")
        return SlashResult(success=True, output=f"{key} = {val}")
    cfg_set(key, parts[1])
    return SlashResult(success=True, output=f"Set {key} = {parts[1]}")


def _cmd_sessions(session_db: SessionDB | None = None, **kwargs: Any) -> SlashResult:
    """List recent sessions."""
    logger.debug("Beginning of _cmd_sessions")
    if session_db is None:
        return SlashResult(success=False, output="No session database available.")
    sessions = session_db.list_sessions(limit=10)
    if not sessions:
        return SlashResult(success=True, output="No sessions found.")
    lines = ["Recent sessions:"]
    for s in sessions:
        lines.append(f"  {s['id'][:8]} — {s.get('title', 'untitled')} ({s.get('updated_at', '')})")
    return SlashResult(success=True, output="\n".join(lines))


def _cmd_tools(**kwargs: Any) -> SlashResult:
    """List available tools."""
    logger.debug("Beginning of _cmd_tools")
    from gyrfalcon.tools import registry
    names = registry.get_tool_names()
    lines = [f"Available tools ({len(names)}):"]
    for name in sorted(names):
        entry = registry.get_entry(name)
        if entry:
            lines.append(f"  {entry.emoji} {name} [{entry.toolset}]")
    return SlashResult(success=True, output="\n".join(lines))


def _cmd_skills(**kwargs: Any) -> SlashResult:
    """List installed skills."""
    logger.debug("Beginning of _cmd_skills")
    from gyrfalcon.tools.skills_tool import _discover_skills
    skills = _discover_skills()
    if not skills:
        return SlashResult(success=True, output="No skills installed.")
    lines = [f"Installed skills ({len(skills)}):"]
    for s in skills:
        lines.append(f"  • {s['name']} — {s.get('description', '')[:60]}")
    return SlashResult(success=True, output="\n".join(lines))


def _cmd_skillhub(args: str = "", **kwargs: Any) -> SlashResult:
    """Manage skill hub registries (add/remove/list)."""
    logger.debug("Beginning of _cmd_skillhub")
    from gyrfalcon_cli.skills_hub import add_registry, remove_registry, list_registries

    parts = args.strip().split(None, 1) if args.strip() else []
    subcommand = parts[0] if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if subcommand == "list":
        registries = list_registries()
        if not registries:
            return SlashResult(success=True, output="No skill hub registries configured.")
        lines = [f"Skill hub registries ({len(registries)}):"]
        for i, reg in enumerate(registries, 1):
            lines.append(f"  {i}. {reg.get('name', '?')}")
            lines.append(f"     url: {reg.get('url', '')}")
            if reg.get("raw_url"):
                lines.append(f"     raw: {reg.get('raw_url', '')}")
        return SlashResult(success=True, output="\n".join(lines))

    elif subcommand == "add":
        if not sub_args:
            return SlashResult(success=False, output="Usage: /skillhub add <name> <api_url> [raw_url]")
        add_parts = sub_args.split()
        if len(add_parts) < 2:
            return SlashResult(success=False, output="Usage: /skillhub add <name> <api_url> [raw_url]")
        name = add_parts[0]
        url = add_parts[1]
        raw_url = add_parts[2] if len(add_parts) > 2 else ""
        result = add_registry(name, url, raw_url)
        if "error" in result:
            return SlashResult(success=False, output=result["error"])
        return SlashResult(success=True, output=f"Added registry '{name}' → {url}")

    elif subcommand == "remove":
        if not sub_args:
            return SlashResult(success=False, output="Usage: /skillhub remove <name>")
        result = remove_registry(sub_args.strip())
        if "error" in result:
            return SlashResult(success=False, output=result["error"])
        return SlashResult(success=True, output=f"Removed registry '{sub_args.strip()}'")

    else:
        return SlashResult(
            success=False,
            output="Usage: /skillhub [list|add|remove]\n"
                   "  list                          — Show configured registries\n"
                   "  add <name> <api_url> [raw]    — Add a registry\n"
                   "  remove <name>                 — Remove a registry",
        )


def _cmd_reasoning(args: str = "", **kwargs: Any) -> SlashResult:
    """Toggle or show the thinking/reasoning display."""
    logger.debug("Beginning of _cmd_reasoning")
    normalized = args.strip().lower()
    if not normalized:
        current = cfg_get("display.show_reasoning", True)
        state = "on" if current else "off"
        return SlashResult(success=True, output=f"Reasoning display is currently {state}. Use /reasoning on|off to toggle.")
    if normalized in ("on", "1", "true", "yes"):
        cfg_set("display.show_reasoning", True)
        return SlashResult(success=True, output="✓ Reasoning display enabled.", data={"show_reasoning": True})
    if normalized in ("off", "0", "false", "no"):
        cfg_set("display.show_reasoning", False)
        return SlashResult(success=True, output="✓ Reasoning display disabled.", data={"show_reasoning": False})
    return SlashResult(success=False, output="Usage: /reasoning [on|off]")



    """Exit the agent."""
    logger.debug("Beginning of _cmd_exit")
    return SlashResult(success=True, output="Goodbye!", data={"action": "exit"})


# Register built-in commands
register_command("help", _cmd_help, "Show available commands", aliases=["h", "?"], category="general")
register_command("clear", _cmd_clear, "Clear session context", aliases=["c"], category="session")
register_command("model", _cmd_model, "Switch or show model", aliases=["m"], category="config")
register_command("compact", _cmd_compact, "Compress context", category="session")
register_command("status", _cmd_status, "Show agent status", aliases=["s"], category="general")
register_command("config", _cmd_config, "View/set configuration", category="config")
register_command("sessions", _cmd_sessions, "List recent sessions", category="session")
register_command("tools", _cmd_tools, "List available tools", aliases=["t"], category="tools")
register_command("skills", _cmd_skills, "List installed skills", category="skills")
register_command("skillhub", _cmd_skillhub, "Manage skill hub registries", aliases=["hub"], category="skills")
register_command("reasoning", _cmd_reasoning, "Toggle thinking/reasoning display", aliases=["think"], category="config")
register_command("exit", _cmd_exit, "Exit the agent", aliases=["quit", "q"], category="general")


class SlashWorker:
    """Async worker that processes slash commands."""

    def __init__(self, session_db: Optional[SessionDB] = None) -> None:
        self.session_db = session_db
        self._queue: asyncio.Queue[tuple[str, asyncio.Future]] = asyncio.Queue()

    async def execute(self, command_text: str) -> SlashResult:
        """Execute a slash command string like '/help' or '/model gpt-4o'."""
        text = command_text.strip()
        if text.startswith("/"):
            text = text[1:]

        parts = text.split(None, 1)
        cmd_name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        cmd_def = get_command(cmd_name)
        if cmd_def is None:
            return SlashResult(
                success=False,
                output=f"Unknown command: /{cmd_name}. Type /help for available commands.",
            )

        try:
            result = cmd_def.handler(
                args=args,
                session_db=self.session_db,
            )
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as e:
            return SlashResult(success=False, output=f"Command error: {e}")
