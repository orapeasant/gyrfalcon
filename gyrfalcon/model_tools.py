"""Tool orchestration — discovery, schema resolution, dispatch."""

from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.tools import registry
from gyrfalcon.toolsets import resolve_multiple_toolsets, _GYRFALCON_CORE_TOOLS

logger = get_logger("model_tools")

_schema_cache: dict[tuple, list[dict]] = {}
_discovered = False


def _discover_from_source(tools_dir: Path) -> int:
    """AST-scan tools/*.py and import only the modules that call registry.register()."""
    imported = 0
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "__init__.py":
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            has_register = any(
                isinstance(node, ast.Call)
                and hasattr(node.func, "attr")
                and node.func.attr == "register"
                for node in ast.walk(tree)
            )
            if has_register:
                importlib.import_module(f"gyrfalcon.tools.{py_file.stem}")
                imported += 1
        except Exception as e:
            logger.warning(f"Failed to import {py_file.name}: {e}")
    return imported


def _discover_from_package() -> int:
    """Import every tool submodule via the import system.

    Required when running from a PyInstaller bundle: the .py sources are inside
    the archive rather than on disk, so globbing the directory finds nothing and
    the agent ends up with an empty tool registry.
    """
    import pkgutil
    from gyrfalcon import tools as tools_pkg

    imported = 0
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"gyrfalcon.tools.{mod.name}")
            imported += 1
        except Exception as e:
            logger.warning(f"Failed to import gyrfalcon.tools.{mod.name}: {e}")
    return imported


def discover_builtin_tools(tools_dir: Path | None = None) -> None:
    """Import the built-in tool modules so they self-register."""
    logger.debug("Beginning of discover_builtin_tools")
    global _discovered
    if _discovered:
        return

    if tools_dir is None:
        tools_dir = Path(__file__).parent / "tools"

    # Source checkout: AST-scan so only modules that register are imported.
    # Frozen bundle: no .py files on disk, so walk the package instead.
    if tools_dir.exists() and any(tools_dir.glob("*.py")):
        imported = _discover_from_source(tools_dir)
    else:
        imported = _discover_from_package()

    if imported == 0:
        # An agent with no tools silently degrades into a chat-only assistant that
        # claims it cannot do things, so make the cause visible.
        logger.error(
            "No built-in tool modules were imported — the agent will have no tools. "
            f"Looked in {tools_dir} (exists={tools_dir.exists()})."
        )

    _discovered = True


def get_enabled_tool_names(
    enabled_toolsets: list[str] | None = None,
    disabled_toolsets: list[str] | None = None,
) -> set[str]:
    """Resolve toolsets to the set of tool names the agent may call."""
    logger.debug("Beginning of get_enabled_tool_names")
    discover_builtin_tools()

    if enabled_toolsets:
        enabled_tools = resolve_multiple_toolsets(enabled_toolsets)
    else:
        enabled_tools = set(_GYRFALCON_CORE_TOOLS)

    if disabled_toolsets:
        enabled_tools -= resolve_multiple_toolsets(disabled_toolsets)
    return enabled_tools


def get_tool_definitions(
    enabled_toolsets: list[str] | None = None,
    disabled_toolsets: list[str] | None = None,
    quiet_mode: bool = False,
) -> list[dict]:
    """Memoized. Resolves toolsets → tool names → schemas."""
    logger.debug("Beginning of get_tool_definitions")
    enabled_tools = get_enabled_tool_names(enabled_toolsets, disabled_toolsets)

    # Cache key
    cache_key = (frozenset(enabled_tools), registry.generation)
    if cache_key in _schema_cache:
        return list(_schema_cache[cache_key])  # copy — never return the cached object directly

    # Get schemas for enabled tools
    schemas = registry.get_schemas(list(enabled_tools))
    _schema_cache[cache_key] = schemas
    # Return a copy — callers may append (e.g. memory schemas) without poisoning the cache
    return list(schemas)


def handle_function_call(
    function_name: str,
    function_args: dict | str,
    task_id: str | None = None,
    tool_call_id: str | None = None,
    session_id: str | None = None,
    user_task: str | None = None,
    enabled_tools: set[str] | None = None,
    skip_pre_tool_call_hook: bool = False,
    plugin_manager=None,
) -> str:
    """Main dispatcher. Coerces args → plugin hooks → registry dispatch → returns JSON string."""
    # Parse args if string
    logger.debug("Beginning of handle_function_call")
    if isinstance(function_args, str):
        try:
            function_args = json.loads(function_args)
        except json.JSONDecodeError:
            function_args = {"input": function_args}

    # Coerce types
    function_args = coerce_tool_args(function_name, function_args)

    # Plugin pre-hook
    if plugin_manager and not skip_pre_tool_call_hook:
        block_result = plugin_manager.fire_hook(
            "pre_tool_call", tool_name=function_name, args=function_args
        )
        if block_result is not None:
            return json.dumps({"blocked": True, "reason": str(block_result)})

    # Dispatch
    result = registry.dispatch(
        function_name, function_args,
        task_id=task_id, tool_call_id=tool_call_id,
        session_id=session_id, user_task=user_task,
    )

    # Plugin post-hook
    if plugin_manager:
        plugin_manager.fire_hook(
            "post_tool_call", tool_name=function_name, args=function_args, result=result
        )
        transformed = plugin_manager.fire_hook(
            "transform_tool_result", tool_name=function_name, result=result
        )
        if transformed is not None:
            result = transformed

    return result


def coerce_tool_args(tool_name: str, args: dict) -> dict:
    """Coerces string arguments to schema-declared types."""
    logger.debug("Beginning of coerce_tool_args")
    entry = registry.get_entry(tool_name)
    if not entry or "parameters" not in entry.schema:
        return args

    properties = entry.schema["parameters"].get("properties", {})
    coerced = {}
    for key, value in args.items():
        if key in properties and isinstance(value, str):
            prop_type = properties[key].get("type")
            try:
                if prop_type == "integer":
                    value = int(value)
                elif prop_type == "number":
                    value = float(value)
                elif prop_type == "boolean":
                    value = value.lower() in ("true", "1", "yes")
                elif prop_type == "array":
                    value = json.loads(value)
                elif prop_type == "object":
                    value = json.loads(value)
            except (ValueError, json.JSONDecodeError):
                pass
        coerced[key] = value
    return coerced
