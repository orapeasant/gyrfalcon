"""Toolset definitions and resolution."""

from __future__ import annotations

from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("toolsets")


# Core tools that all platforms inherit
_GYRFALCON_CORE_TOOLS: set[str] = {
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "web_search",
    "web_extract",
    "delegate_task",
    "execute_code",
    "session_search",
    "skills_list",
    "skill_view",
    "skill_manage",
    "memory",
    "todo",
    "scheduler",
    "clarify",
}

# Toolset definitions
TOOLSETS: dict[str, dict] = {
    "terminal": {
        "tools": ["terminal"],
        "description": "Shell command execution",
    },
    "file": {
        "tools": ["read_file", "write_file", "patch", "search_files"],
        "description": "File operations",
    },
    "web": {
        "tools": ["web_search", "web_extract"],
        "description": "Web search and content extraction",
    },
    "browser": {
        "tools": [
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_back",
            "browser_press", "browser_get_images", "browser_vision",
            "browser_console",
        ],
        "description": "Browser automation",
    },
    "vision": {
        "tools": ["vision_analyze"],
        "description": "Image analysis",
    },
    "video": {
        "tools": ["video_analyze"],
        "description": "Video analysis",
    },
    "delegation": {
        "tools": ["delegate_task"],
        "description": "Spawn child agents",
    },
    "code_execution": {
        "tools": ["execute_code"],
        "description": "Sandboxed code execution",
    },
    "session_search": {
        "tools": ["session_search"],
        "description": "Search past sessions",
    },
    "skills": {
        "tools": ["skills_list", "skill_view", "skill_manage"],
        "description": "Skill management",
    },
    "memory": {
        "tools": ["memory"],
        "description": "Memory operations",
    },
    "todo": {
        "tools": ["todo"],
        "description": "Task tracking",
    },
    "scheduler": {
        "tools": ["scheduler"],
        "description": "Scheduled tasks",
    },
    "core": {
        "tools": list(_GYRFALCON_CORE_TOOLS),
        "description": "All core tools",
    },
    # What an agent reached over a chat platform may do (spec 18-slack.md §6.1,
    # D3). Read-and-ask: research, recall, scheduling, delegation. Deliberately
    # absent: terminal, execute_code, write_file, patch — anything that changes
    # the machine — and skill_manage, which writes skill files to disk. Other
    # people's text reaches the model on these platforms, so the set is a
    # security boundary, and is enforced at dispatch (model_tools.py), not only
    # in the schema shown to the model.
    "gateway_safe": {
        "tools": [
            "read_file", "search_files", "session_search",
            "web_search", "web_extract",
            "memory", "todo", "skills_list", "skill_view",
            "scheduler", "delegate_task", "clarify",
        ],
        "description": "Restricted set for chat-platform agents (no shell, no writes)",
    },
    "slack": {
        "includes": ["gateway_safe"],
        "description": "Slack — the restricted chat-platform set",
    },
    "teams": {
        "includes": ["gateway_safe"],
        "description": "Microsoft Teams — the restricted chat-platform set",
    },
    "all": {
        "includes": ["core", "browser", "vision", "video"],
        "description": "All available tools",
    },
}


def resolve_toolset(name: str, visited: set | None = None) -> set[str]:
    """Recursive resolution with cycle detection."""
    logger.debug("Beginning of resolve_toolset")
    if visited is None:
        visited = set()
    if name in visited:
        return set()
    visited.add(name)

    if name == "*":
        name = "all"

    toolset = TOOLSETS.get(name)
    if not toolset:
        # Not a static toolset — check for a dynamically-registered one, e.g.
        # "mcp-{server_name}" (stdio tools discovered at connect time,
        # openapi tools registered at startup; see mcp_tool.py /
        # openapi_mcp_tool.py). Falling straight through to "treat as an
        # individual tool name" here is exactly why an agent's attached MCP
        # servers previously resolved to nothing: "mcp-foo" is a toolset
        # name, not a tool name, and get_schemas() silently drops names it
        # doesn't recognize.
        from gyrfalcon.tools import registry
        dynamic = registry.get_tool_names_for_toolset(name)
        if dynamic:
            return set(dynamic)
        return {name}  # Treat as individual tool name

    tools: set[str] = set()
    if "tools" in toolset:
        tools.update(toolset["tools"])
    if "includes" in toolset:
        for included in toolset["includes"]:
            tools.update(resolve_toolset(included, visited))
    return tools


def resolve_multiple_toolsets(toolset_names: list[str]) -> set[str]:
    """Union of multiple resolved toolsets."""
    logger.debug("Beginning of resolve_multiple_toolsets")
    result: set[str] = set()
    for name in toolset_names:
        result.update(resolve_toolset(name))
    return result


def get_toolset(name: str) -> Optional[dict]:
    """Returns toolset definition dict."""
    logger.debug("Beginning of get_toolset")
    return TOOLSETS.get(name)


def list_toolsets() -> list[str]:
    """List all available toolset names."""
    logger.debug("Beginning of list_toolsets")
    return list(TOOLSETS.keys())


#: Toolset an agent gets on a chat platform when neither the platform's config
#: nor a routing rule names one. Restrictive on purpose: forgetting to configure
#: a new adapter must fail closed, not hand it a shell.
GATEWAY_DEFAULT_TOOLSET = "gateway_safe"

#: What a user in `allow.elevated` gets instead. The full core set, including
#: the shell — reachable only because every dangerous call in it is now put to a
#: person before it runs (spec 18-slack.md D3, Phase 2).
ELEVATED_DEFAULT_TOOLSET = "core"

#: Tools that change the machine or run arbitrary code. Naming a toolset that
#: resolves to any of these on a chat platform is allowed — it is the operator's
#: call — but is loud, because it makes the allowlist the only thing between a
#: Slack message and a shell.
DANGEROUS_TOOLS: frozenset[str] = frozenset({"terminal", "execute_code", "write_file", "patch", "skill_manage"})


def dangerous_tools_in(toolset_names: list[str]) -> set[str]:
    """Which of `DANGEROUS_TOOLS` the given toolsets would expose."""
    return resolve_multiple_toolsets(toolset_names) & DANGEROUS_TOOLS
