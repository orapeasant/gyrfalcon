"""CLI commands registry — central registry of all CLI commands/subcommands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.gyrfalcon_constants import get_app_name
logger = get_logger("commands")



@dataclass
class CommandDef:
    """CLI command definition."""
    name: str
    handler: Callable
    description: str
    aliases: list[str]
    category: str
    usage: str = ""
    hidden: bool = False


_registry: dict[str, CommandDef] = {}


def register(
    name: str,
    handler: Callable,
    description: str = "",
    aliases: list[str] | None = None,
    category: str = "general",
    usage: str = "",
    hidden: bool = False,
) -> None:
    """Register a CLI command."""
    logger.debug("Beginning of register")
    cmd = CommandDef(
        name=name,
        handler=handler,
        description=description,
        aliases=aliases or [],
        category=category,
        usage=usage,
        hidden=hidden,
    )
    _registry[name] = cmd
    for alias in cmd.aliases:
        _registry[alias] = cmd


def get(name: str) -> Optional[CommandDef]:
    """Look up command by name or alias."""
    logger.debug("Beginning of get")
    return _registry.get(name)


def list_all(include_hidden: bool = False) -> list[CommandDef]:
    """List all commands (deduplicated)."""
    logger.debug("Beginning of list_all")
    seen = set()
    result = []
    for cmd in _registry.values():
        if cmd.name not in seen:
            if include_hidden or not cmd.hidden:
                result.append(cmd)
                seen.add(cmd.name)
    return sorted(result, key=lambda c: (c.category, c.name))


def list_by_category() -> dict[str, list[CommandDef]]:
    """Group commands by category."""
    logger.debug("Beginning of list_by_category")
    categories: dict[str, list[CommandDef]] = {}
    for cmd in list_all():
        categories.setdefault(cmd.category, []).append(cmd)
    return categories


# Register all built-in commands
def _register_builtins() -> None:
    """Register all built-in CLI commands."""
    logger.debug("Beginning of _register_builtins")
    from gyrfalcon_cli.auth import run_auth_cli
    from gyrfalcon_cli.doctor import run_doctor
    from gyrfalcon_cli.gateway_cmd import run_gateway
    from gyrfalcon_cli.scheduler_cmd import run_scheduler_cli
    from gyrfalcon_cli.skills_cmd import run_skills_cli
    from gyrfalcon_cli.logs_cmd import run_logs_cli
    from gyrfalcon_cli.tools_config import run_tools_config

    register("chat", lambda args: None, "Start interactive chat", category="core", usage="gyrfalcon chat [session_id]")
    register("auth", run_auth_cli, "Manage authentication", aliases=["login"], category="auth", usage="gyrfalcon auth [login|logout|status|switch]")
    register("doctor", lambda args: run_doctor(), "Run diagnostics", category="system", usage="gyrfalcon doctor")
    register("gateway", run_gateway, "Start gateway server", category="server", usage="gyrfalcon gateway [start|stop|status]")
    register("scheduler", run_scheduler_cli, "Manage scheduled jobs", aliases=["cron"], category="automation", usage="gyrfalcon scheduler [list|add|remove|status]")
    register("skills", run_skills_cli, "Manage skills", category="skills", usage="gyrfalcon skills [list|install|remove|info]")
    register("logs", run_logs_cli, "View logs", category="system", usage="gyrfalcon logs [--tail] [--level]")
    register("tools", run_tools_config, "Configure tools", category="tools", usage="gyrfalcon tools [list|enable|disable|info]")
    register("version", lambda args: _print_version(), "Show version", aliases=["--version", "-v"], category="system")
    register("help", lambda args: _print_help(), "Show help", aliases=["--help", "-h"], category="system")


def _print_version() -> None:
    logger.debug("Beginning of _print_version")
    from gyrfalcon import __version__
    print(f"{get_app_name()} v{__version__}")


def _print_help() -> None:
    logger.debug("Beginning of _print_help")
    print(f"{get_app_name()} — AI Agent\n")
    print("Usage: gyrfalcon <command> [options]\n")
    categories = list_by_category()
    for cat, cmds in sorted(categories.items()):
        print(f"  [{cat}]")
        for cmd in cmds:
            alias_str = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
            print(f"    {cmd.name}{alias_str:20s} {cmd.description}")
        print()
