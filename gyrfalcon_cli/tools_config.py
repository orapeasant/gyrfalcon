"""CLI tools config — configure tool-specific settings and permissions."""

from __future__ import annotations

from gyrfalcon.config import cfg_get, cfg_set
from gyrfalcon.tools import registry
from gyrfalcon.model_tools import discover_builtin_tools
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("tools_config")



def run_tools_config(args: list[str]) -> None:
    """Handle `gyrfalcon tools` subcommands."""
    logger.debug("Beginning of run_tools_config")
    if not args:
        _list_tools()
        return

    subcmd = args[0]
    if subcmd == "list":
        _list_tools()
    elif subcmd == "enable":
        _toggle_tool(args[1:], enable=True)
    elif subcmd == "disable":
        _toggle_tool(args[1:], enable=False)
    elif subcmd == "info":
        _tool_info(args[1:])
    elif subcmd == "config":
        _tool_config(args[1:])
    elif subcmd == "approval":
        _tool_approval(args[1:])
    else:
        print(f"Unknown tools command: {subcmd}")
        print("Usage: gyrfalcon tools [list|enable|disable|info|config|approval]")


def _list_tools() -> None:
    """List all available tools with status."""
    logger.debug("Beginning of _list_tools")
    discover_builtin_tools()
    names = registry.get_tool_names()
    disabled = cfg_get("tools.disabled", [])

    print(f"Available tools ({len(names)}):\n")
    by_toolset: dict[str, list] = {}
    for name in sorted(names):
        entry = registry.get_entry(name)
        if entry:
            by_toolset.setdefault(entry.toolset, []).append(entry)

    for toolset, entries in sorted(by_toolset.items()):
        print(f"  [{toolset}]")
        for entry in entries:
            status = "✗" if entry.name in disabled else "✓"
            print(f"    {status} {entry.emoji} {entry.name} — {entry.description[:50]}")
        print()


def _toggle_tool(args: list[str], enable: bool) -> None:
    """Enable or disable a tool."""
    logger.debug("Beginning of _toggle_tool")
    if not args:
        action = "enable" if enable else "disable"
        print(f"Usage: gyrfalcon tools {action} <tool_name>")
        return

    tool_name = args[0]
    disabled: list = cfg_get("tools.disabled", [])

    if enable:
        if tool_name in disabled:
            disabled.remove(tool_name)
            cfg_set("tools.disabled", disabled)
            print(f"✓ Enabled tool: {tool_name}")
        else:
            print(f"Tool '{tool_name}' is already enabled")
    else:
        if tool_name not in disabled:
            disabled.append(tool_name)
            cfg_set("tools.disabled", disabled)
            print(f"✓ Disabled tool: {tool_name}")
        else:
            print(f"Tool '{tool_name}' is already disabled")


def _tool_info(args: list[str]) -> None:
    """Show detailed info about a tool."""
    logger.debug("Beginning of _tool_info")
    if not args:
        print("Usage: gyrfalcon tools info <tool_name>")
        return

    discover_builtin_tools()
    tool_name = args[0]
    entry = registry.get_entry(tool_name)
    if not entry:
        print(f"Tool not found: {tool_name}")
        return

    print(f"{entry.emoji} {entry.name}")
    print(f"  Toolset: {entry.toolset}")
    print(f"  Description: {entry.description}")
    print(f"  Async: {entry.is_async}")
    if entry.parameters:
        print(f"  Parameters:")
        props = entry.parameters.get("properties", {})
        required = entry.parameters.get("required", [])
        for pname, pschema in props.items():
            req_mark = "*" if pname in required else " "
            ptype = pschema.get("type", "any")
            pdesc = pschema.get("description", "")
            print(f"    {req_mark} {pname}: {ptype} — {pdesc[:40]}")


def _tool_config(args: list[str]) -> None:
    """Configure tool-specific settings."""
    logger.debug("Beginning of _tool_config")
    if len(args) < 2:
        print("Usage: gyrfalcon tools config <tool_name> <key>=<value>")
        return

    tool_name = args[0]
    for setting in args[1:]:
        if "=" not in setting:
            print(f"Invalid setting format: {setting} (use key=value)")
            continue
        key, value = setting.split("=", 1)
        cfg_set(f"tools.{tool_name}.{key}", value)
        print(f"✓ Set {tool_name}.{key} = {value}")


def _tool_approval(args: list[str]) -> None:
    """Configure approval settings for tools."""
    logger.debug("Beginning of _tool_approval")
    if not args:
        # Show current approval settings
        mode = cfg_get("approval.mode", "smart")
        always_approve = cfg_get("approval.always_approve", [])
        never_approve = cfg_get("approval.never_approve", [])
        print(f"Approval mode: {mode}")
        print(f"  Modes: always_ask | smart | auto_approve")
        if always_approve:
            print(f"  Always approve: {', '.join(always_approve)}")
        if never_approve:
            print(f"  Never approve: {', '.join(never_approve)}")
        return

    if args[0] == "mode" and len(args) > 1:
        mode = args[1]
        if mode in ("always_ask", "smart", "auto_approve"):
            cfg_set("approval.mode", mode)
            print(f"✓ Approval mode set to: {mode}")
        else:
            print("Valid modes: always_ask | smart | auto_approve")
    elif args[0] == "allow" and len(args) > 1:
        allowed: list = cfg_get("approval.always_approve", [])
        tool_name = args[1]
        if tool_name not in allowed:
            allowed.append(tool_name)
            cfg_set("approval.always_approve", allowed)
        print(f"✓ Always approve: {tool_name}")
    elif args[0] == "deny" and len(args) > 1:
        denied: list = cfg_get("approval.never_approve", [])
        tool_name = args[1]
        if tool_name not in denied:
            denied.append(tool_name)
            cfg_set("approval.never_approve", denied)
        print(f"✓ Never approve: {tool_name}")
