"""Configuration management — YAML config + .env file handling."""

import os
import functools
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import dotenv_values, set_key

from gyrfalcon.gyrfalcon_constants import get_config_path, get_env_path, get_gyrfalcon_home
from gyrfalcon.utils import atomic_yaml_write
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("config")


DEFAULT_CONFIG: dict[str, Any] = {
    "_config_version": 1,
    "model": {
        "name": "",
        "context_length": None,
        "max_tokens": None,
        "temperature": None,
        "reasoning_effort": None,
    },
    "agent": {
        "max_turns": 90,
        "max_llm_call": 20,       # hard cap on LLM API calls per conversation turn
        "gateway_timeout": 1200,
        "save_trajectories": False,
    },
    "terminal": {
        "backend": "local",
        "cwd": None,
        "sandbox_dir": None,
        "env_passthrough_vars": [],
        "persistent_shell": True,
    },
    "compression": {
        "threshold_percent": 0.50,
        "protect_first_n": 3,
    },
    "display": {
        "skin": "default",
        "streaming": True,
        "compact": False,
        "show_reasoning": True,
        "bell_on_complete": False,
    },
    "performance": {
        "telemetry_enabled": False,
        "stream_flush_enabled": False,
    },
    "memory": {
        "provider": "builtin",
    },
    "security": {
        "approval_mode": "smart",
    },
    "delegation": {
        "max_concurrent_children": 3,
        "max_spawn_depth": 2,
    },
    "gateway": {
        "platforms": {},
    },
    "scheduler": {
        "enabled": True,
    },
    "plugins": {
        "enabled": [],
        "disabled": [],
    },
    "web": {
        "port": 9119,
        "host": "127.0.0.1",
    },
    "network": {
        "proxy": "",
    },
    "logging": {
        "level": "ERROR",
    },
    "auxiliary": {},
}

OPTIONAL_ENV_VARS: dict[str, dict] = {
    "OPENAI_API_KEY": {"description": "OpenAI API key", "category": "provider", "password": True},
    "ANTHROPIC_API_KEY": {"description": "Anthropic API key", "category": "provider", "password": True},
    "OPENROUTER_API_KEY": {"description": "OpenRouter API key", "category": "provider", "password": True},
    "AWS_ACCESS_KEY_ID": {"description": "AWS Access Key ID", "category": "provider", "password": False},
    "AWS_SECRET_ACCESS_KEY": {"description": "AWS Secret Access Key", "category": "provider", "password": True},
    "AWS_DEFAULT_REGION": {"description": "AWS Default Region", "category": "provider", "password": False},
    "GITHUB_TOKEN": {"description": "GitHub token for Copilot auth", "category": "provider", "password": True},
    "FIRECRAWL_API_KEY": {"description": "Firecrawl API key for web extraction", "category": "tool", "password": True},
    "EXA_API_KEY": {"description": "Exa search API key", "category": "tool", "password": True},
    "TAVILY_API_KEY": {"description": "Tavily search API key", "category": "tool", "password": True},
}

_config_cache: dict[str, Any] = {}
_config_mtime: float = 0.0


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base."""
    logger.debug("Beginning of _deep_merge")
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    """Load ~/.gyrfalcon/config.yaml merged with DEFAULT_CONFIG. Cached by mtime."""
    logger.debug("Beginning of load_config")
    global _config_cache, _config_mtime

    config_path = get_config_path()
    if not config_path.exists():
        return DEFAULT_CONFIG.copy()

    try:
        mtime = config_path.stat().st_mtime
        if mtime == _config_mtime and _config_cache:
            return _config_cache

        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}

        # Migrate old 'cron' key → 'scheduler'
        if "cron" in user_config and "scheduler" not in user_config:
            user_config["scheduler"] = user_config.pop("cron")

        merged = _deep_merge(DEFAULT_CONFIG, user_config)
        _config_cache = merged
        _config_mtime = mtime
        return merged
    except (OSError, yaml.YAMLError):
        return DEFAULT_CONFIG.copy()


def save_config(config: dict[str, Any]) -> None:
    """Save config to disk."""
    logger.debug("Beginning of save_config")
    global _config_cache, _config_mtime
    config_path = get_config_path()
    atomic_yaml_write(config_path, config)
    _config_mtime = config_path.stat().st_mtime
    _config_cache = config


def cfg_get(dotpath: str, default: Any = None) -> Any:
    """Get a config value by dot-separated path."""
    logger.debug("Beginning of cfg_get")
    config = load_config()
    keys = dotpath.split(".")
    current = config
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def cfg_set(dotpath: str, value: Any) -> None:
    """Set a config value by dot-separated path and persist."""
    logger.debug("Beginning of cfg_set")
    config = load_config()
    keys = dotpath.split(".")
    current = config
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    save_config(config)


def save_env_value(key: str, value: str) -> None:
    """Save an environment variable to .env file."""
    logger.debug("Beginning of save_env_value")
    env_path = get_env_path()
    if not env_path.exists():
        env_path.touch(mode=0o600)
    set_key(str(env_path), key, value)
    os.environ[key] = value


def get_env_value(key: str) -> Optional[str]:
    """Get env value from .env file or environment."""
    logger.debug("Beginning of get_env_value")
    val = os.environ.get(key)
    if val:
        return val
    env_path = get_env_path()
    if env_path.exists():
        values = dotenv_values(str(env_path))
        return values.get(key)
    return None


def load_env_file() -> None:
    """Load .env file into environment."""
    logger.debug("Beginning of load_env_file")
    env_path = get_env_path()
    if env_path.exists():
        values = dotenv_values(str(env_path))
        for key, value in values.items():
            if value is not None and key not in os.environ:
                os.environ[key] = value


def get_all_env_vars() -> dict[str, str]:
    """Get all env vars from .env file."""
    logger.debug("Beginning of get_all_env_vars")
    env_path = get_env_path()
    if env_path.exists():
        return {k: v for k, v in dotenv_values(str(env_path)).items() if v is not None}
    return {}
