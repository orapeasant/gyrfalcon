"""Skin engine — data-driven CLI theming."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_app_name
from gyrfalcon.config import cfg_get
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("skin_engine")


_active_skin: Optional["SkinConfig"] = None


@dataclass
class SkinConfig:
    name: str = "default"
    # Banner colors
    border_color: str = "bright_yellow"
    title_color: str = "bold bright_yellow"
    accent_color: str = "yellow"
    dim_color: str = "dim"
    text_color: str = "white"
    # Spinner
    spinner_faces: list[str] = field(default_factory=lambda: ["(◕‿◕)", "(◠‿◠)", "(◕ᴗ◕)"])
    spinner_verbs: list[str] = field(default_factory=lambda: ["thinking", "pondering", "considering"])
    # Branding
    agent_name: str = field(default_factory=get_app_name)
    welcome_message: str = "AI Agent"
    prompt_symbol: str = "❯"
    # Tool emojis
    tool_emojis: dict[str, str] = field(default_factory=lambda: {
        "terminal": "💻",
        "read_file": "📖",
        "write_file": "✏️",
        "web_search": "🌐",
        "web_extract": "📄",
        "delegate_task": "🔀",
        "execute_code": "🐍",
        "skills_list": "📚",
        "memory": "🧠",
        "todo": "📋",
        "scheduler": "⏰",
    })


# Built-in skins
BUILTIN_SKINS: dict[str, dict] = {
    "default": {
        "name": "default",
        "border_color": "bright_yellow",
        "title_color": "bold bright_yellow",
    },
    "mono": {
        "name": "mono",
        "border_color": "white",
        "title_color": "bold white",
        "accent_color": "white",
    },
    "slate": {
        "name": "slate",
        "border_color": "bright_blue",
        "title_color": "bold bright_blue",
        "accent_color": "blue",
    },
}


def get_active_skin() -> SkinConfig:
    """Get the active skin configuration."""
    logger.debug("Beginning of get_active_skin")
    global _active_skin
    if _active_skin is None:
        skin_name = cfg_get("display.skin", "default")
        _active_skin = load_skin(skin_name)
    return _active_skin


def set_active_skin(name: str) -> None:
    """Set the active skin."""
    logger.debug("Beginning of set_active_skin")
    global _active_skin
    _active_skin = load_skin(name)


def load_skin(name: str) -> SkinConfig:
    """Load skin: user skins → built-in → default fallback."""
    # Check user skins
    logger.debug("Beginning of load_skin")
    user_skin_path = get_gyrfalcon_home() / "skins" / f"{name}.yaml"
    if user_skin_path.exists():
        try:
            data = yaml.safe_load(user_skin_path.read_text())
            return _dict_to_skin(data)
        except (OSError, yaml.YAMLError):
            pass

    # Check built-in
    if name in BUILTIN_SKINS:
        return _dict_to_skin(BUILTIN_SKINS[name])

    # Fallback to default
    return SkinConfig()


def _dict_to_skin(data: dict) -> SkinConfig:
    """Convert dict to SkinConfig, filling defaults."""
    logger.debug("Beginning of _dict_to_skin")
    config = SkinConfig()
    for key, value in data.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return config
