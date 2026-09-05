"""Plugin system — discovery, hooks, and lifecycle management."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_plugins_dir
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("plugins")

HOOK_NAMES = [
    "pre_tool_call",
    "post_tool_call",
    "transform_tool_result",
    "pre_llm_call",
    "post_llm_call",
    "on_session_start",
    "on_session_end",
]


@dataclass
class PluginManifest:
    name: str
    version: str = "0.0.1"
    description: str = ""
    kind: str = "general"  # general, model-provider, memory, context-engine
    author: str = ""


class PluginContext:
    """Context passed to plugin register() function."""

    def __init__(self, plugin_name: str, manager: "PluginManager"):
        self.plugin_name = plugin_name
        self._manager = manager

    def register_hook(self, hook_name: str, handler: Callable) -> None:
        """Register a lifecycle hook."""
        logger.debug("Beginning of register_hook")
        if hook_name not in HOOK_NAMES:
            logger.warning(f"Plugin {self.plugin_name}: unknown hook '{hook_name}'")
            return
        self._manager._hooks[hook_name].append((self.plugin_name, handler))

    def register_tool(
        self, name: str, schema: dict, handler: Callable, toolset: str | None = None
    ) -> None:
        """Register a tool from a plugin."""
        logger.debug("Beginning of register_tool")
        from gyrfalcon.tools import registry
        registry.register(
            name=name,
            toolset=toolset or f"plugin-{self.plugin_name}",
            schema=schema,
            handler=handler,
        )
        self._manager._plugin_tools[self.plugin_name].append(name)

    def register_cli_command(
        self, name: str, parser_setup: Callable, handler: Callable
    ) -> None:
        """Register a CLI subcommand."""
        logger.debug("Beginning of register_cli_command")
        self._manager._cli_commands[name] = {
            "plugin": self.plugin_name,
            "parser_setup": parser_setup,
            "handler": handler,
        }


class PluginManager:
    """Manages plugin discovery, loading, and hook dispatch."""

    def __init__(self):
        self._plugins: dict[str, PluginManifest] = {}
        self._hooks: dict[str, list[tuple[str, Callable]]] = {name: [] for name in HOOK_NAMES}
        self._plugin_tools: dict[str, list[str]] = {}
        self._cli_commands: dict[str, dict] = {}
        self._loaded: set[str] = set()
        self._lock = threading.Lock()

    def discover_and_load(self, extra_dirs: list[Path] | None = None) -> None:
        """Discover and load plugins from all sources."""
        logger.debug("Beginning of discover_and_load")
        search_dirs = [
            Path(__file__).parent / "plugins",  # Bundled
            get_plugins_dir(),  # User plugins
        ]

        # Project-local plugins
        project_plugins = Path.cwd() / ".gyrfalcon" / "plugins"
        if project_plugins.exists():
            search_dirs.append(project_plugins)

        if extra_dirs:
            search_dirs.extend(extra_dirs)

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for plugin_dir in sorted(search_dir.iterdir()):
                if plugin_dir.is_dir() and (plugin_dir / "plugin.yaml").exists():
                    self._load_plugin(plugin_dir)

        # Entry points
        self._discover_entry_points()

    def _load_plugin(self, plugin_dir: Path) -> None:
        """Load a single plugin from directory."""
        logger.debug("Beginning of _load_plugin")
        manifest_path = plugin_dir / "plugin.yaml"
        try:
            manifest_data = yaml.safe_load(manifest_path.read_text())
            manifest = PluginManifest(
                name=manifest_data.get("name", plugin_dir.name),
                version=manifest_data.get("version", "0.0.1"),
                description=manifest_data.get("description", ""),
                kind=manifest_data.get("kind", "general"),
                author=manifest_data.get("author", ""),
            )
        except (OSError, yaml.YAMLError) as e:
            logger.warning(f"Failed to read plugin manifest at {manifest_path}: {e}")
            return

        if manifest.name in self._loaded:
            return

        # Find and execute register()
        init_file = plugin_dir / "__init__.py"
        main_file = plugin_dir / "main.py"
        register_file = init_file if init_file.exists() else main_file

        if not register_file.exists():
            logger.warning(f"Plugin {manifest.name}: no __init__.py or main.py found")
            return

        try:
            spec = importlib.util.spec_from_file_location(
                f"gyrfalcon_plugin_{manifest.name}", str(register_file)
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"gyrfalcon_plugin_{manifest.name}"] = module
                spec.loader.exec_module(module)

                if hasattr(module, "register"):
                    ctx = PluginContext(manifest.name, self)
                    self._plugin_tools[manifest.name] = []
                    module.register(ctx)
                    self._plugins[manifest.name] = manifest
                    self._loaded.add(manifest.name)
                    logger.info(f"Loaded plugin: {manifest.name} v{manifest.version}")
                else:
                    logger.warning(f"Plugin {manifest.name}: no register() function")
        except Exception as e:
            logger.error(f"Failed to load plugin {manifest.name}: {e}", exc_info=True)

    def _discover_entry_points(self) -> None:
        """Discover plugins via pip entry points."""
        logger.debug("Beginning of _discover_entry_points")
        try:
            if sys.version_info >= (3, 12):
                from importlib.metadata import entry_points
                eps = entry_points(group="gyrfalcon.plugins")
            else:
                from importlib.metadata import entry_points
                eps = entry_points().get("gyrfalcon.plugins", [])

            for ep in eps:
                if ep.name in self._loaded:
                    continue
                try:
                    module = ep.load()
                    if hasattr(module, "register"):
                        ctx = PluginContext(ep.name, self)
                        self._plugin_tools[ep.name] = []
                        module.register(ctx)
                        self._plugins[ep.name] = PluginManifest(name=ep.name)
                        self._loaded.add(ep.name)
                        logger.info(f"Loaded entry-point plugin: {ep.name}")
                except Exception as e:
                    logger.warning(f"Failed to load entry-point plugin {ep.name}: {e}")
        except Exception:
            pass

    def fire_hook(self, hook_name: str, **kwargs) -> Any:
        """Fire a hook. Returns first non-None result (for blocking hooks)."""
        logger.debug("Beginning of fire_hook")
        handlers = self._hooks.get(hook_name, [])
        for plugin_name, handler in handlers:
            try:
                result = handler(**kwargs)
                if result is not None and hook_name in ("pre_tool_call", "transform_tool_result"):
                    return result
            except Exception as e:
                logger.error(f"Plugin {plugin_name} hook {hook_name} failed: {e}")
        return None

    def get_cli_commands(self) -> dict[str, dict]:
        logger.debug("Beginning of get_cli_commands")
        return self._cli_commands

    def list_plugins(self) -> list[PluginManifest]:
        logger.debug("Beginning of list_plugins")
        return list(self._plugins.values())

    def is_loaded(self, name: str) -> bool:
        logger.debug("Beginning of is_loaded")
        return name in self._loaded
