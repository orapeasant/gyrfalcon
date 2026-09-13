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
