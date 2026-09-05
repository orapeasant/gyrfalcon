"""Skill-related slash commands — parse and execute skill management actions."""

from __future__ import annotations

from typing import Any

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("agent.skill_commands")

_VALID_ACTIONS = frozenset({
    "list", "view", "enable", "disable", "install", "uninstall", "search"
})


def parse_skill_command(text: str) -> dict[str, Any]:
    """Parse a skill slash command into an action and arguments.

    Expected formats:
        /skill list
        /skill view <name>
        /skill enable <name>
        /skill disable <name>
        /skill install <source>
        /skill uninstall <name>
        /skill search <query>

    Args:
        text: The raw command text (with or without /skill prefix).

    Returns:
        Dict with keys:
            - action: str — one of the valid actions
            - args: list[str] — positional arguments after the action
            - raw: str — the original text

    Raises:
        ValueError: If the command cannot be parsed or action is invalid.
    """
    logger.debug("Beginning of parse_skill_command")
    raw = text.strip()

    # Strip the /skill prefix if present
    normalized = raw
    if normalized.startswith("/skill"):
        normalized = normalized[len("/skill"):].strip()
    elif normalized.startswith("skill"):
        normalized = normalized[len("skill"):].strip()

    if not normalized:
        raise ValueError(
            f"No action specified. Valid actions: {', '.join(sorted(_VALID_ACTIONS))}"
        )

    parts = normalized.split()
    action = parts[0].lower()
    args = parts[1:]

    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"Unknown skill action: '{action}'. "
            f"Valid actions: {', '.join(sorted(_VALID_ACTIONS))}"
        )

    # Validate required arguments
    if action in ("view", "enable", "disable", "uninstall") and not args:
        raise ValueError(f"Action '{action}' requires a skill name argument.")
    if action == "install" and not args:
        raise ValueError("Action 'install' requires a source argument.")
    if action == "search" and not args:
        raise ValueError("Action 'search' requires a query argument.")

    return {"action": action, "args": args, "raw": raw}


def execute_skill_command(
    action: str,
    args: list[str],
    session_db: Any,
) -> str:
    """Execute a parsed skill command against the session database.

    Args:
        action: The skill action to perform.
        args: Arguments for the action.
        session_db: Database/store interface with skill management methods.
            Expected interface:
                - list_skills() -> list[dict]
                - get_skill(name) -> dict | None
                - set_skill_enabled(name, enabled: bool) -> bool
                - install_skill(source) -> dict
                - uninstall_skill(name) -> bool
                - search_skills(query) -> list[dict]

    Returns:
        Human-readable result message.
    """
    logger.debug("Beginning of execute_skill_command")
    try:
        if action == "list":
            return _execute_list(session_db)
        elif action == "view":
            return _execute_view(args[0], session_db)
        elif action == "enable":
            return _execute_enable(args[0], session_db)
        elif action == "disable":
            return _execute_disable(args[0], session_db)
        elif action == "install":
            return _execute_install(args[0], session_db)
        elif action == "uninstall":
            return _execute_uninstall(args[0], session_db)
        elif action == "search":
            query = " ".join(args)
            return _execute_search(query, session_db)
        else:
            return f"Unknown action: {action}"
    except Exception as e:
        logger.error(f"Skill command '{action}' failed: {e}")
        return f"Error executing '{action}': {e}"


def _execute_list(session_db: Any) -> str:
    """List all installed skills."""
    logger.debug("Beginning of _execute_list")
    skills = session_db.list_skills()
    if not skills:
        return "No skills installed."

    lines: list[str] = ["Installed skills:", ""]
    for skill in skills:
        name = skill.get("name", "unknown")
        enabled = "✓" if skill.get("enabled", True) else "✗"
        description = skill.get("description", "")
        lines.append(f"  [{enabled}] {name} — {description}")
    return "\n".join(lines)


def _execute_view(name: str, session_db: Any) -> str:
    """View details of a specific skill."""
    logger.debug("Beginning of _execute_view")
    skill = session_db.get_skill(name)
    if skill is None:
        return f"Skill '{name}' not found."

    lines: list[str] = [
        f"Skill: {skill.get('name', name)}",
        f"  Enabled: {skill.get('enabled', True)}",
        f"  Version: {skill.get('version', 'unknown')}",
        f"  Source:  {skill.get('source', 'local')}",
        f"  Description: {skill.get('description', 'N/A')}",
    ]
    tools = skill.get("tools", [])
    if tools:
        lines.append(f"  Tools: {', '.join(tools)}")
    return "\n".join(lines)


def _execute_enable(name: str, session_db: Any) -> str:
    """Enable a skill."""
    logger.debug("Beginning of _execute_enable")
    success = session_db.set_skill_enabled(name, enabled=True)
    if success:
        return f"Skill '{name}' enabled."
    return f"Failed to enable skill '{name}'. Is it installed?"


def _execute_disable(name: str, session_db: Any) -> str:
    """Disable a skill."""
    logger.debug("Beginning of _execute_disable")
    success = session_db.set_skill_enabled(name, enabled=False)
    if success:
        return f"Skill '{name}' disabled."
    return f"Failed to disable skill '{name}'. Is it installed?"


def _execute_install(source: str, session_db: Any) -> str:
    """Install a skill from a source."""
    logger.debug("Beginning of _execute_install")
    result = session_db.install_skill(source)
    name = result.get("name", source)
    return f"Skill '{name}' installed successfully."


def _execute_uninstall(name: str, session_db: Any) -> str:
    """Uninstall a skill."""
    logger.debug("Beginning of _execute_uninstall")
    success = session_db.uninstall_skill(name)
    if success:
        return f"Skill '{name}' uninstalled."
    return f"Failed to uninstall skill '{name}'. Is it installed?"


def _execute_search(query: str, session_db: Any) -> str:
    """Search for available skills."""
    logger.debug("Beginning of _execute_search")
    results = session_db.search_skills(query)
    if not results:
        return f"No skills found matching '{query}'."

    lines: list[str] = [f"Search results for '{query}':", ""]
    for skill in results:
        name = skill.get("name", "unknown")
        description = skill.get("description", "")
        lines.append(f"  • {name} — {description}")
    return "\n".join(lines)
