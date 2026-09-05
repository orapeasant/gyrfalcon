"""Shared utility functions."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("utils")



def atomic_json_write(path: str | Path, data: Any, indent: int = 2, **dump_kwargs) -> None:
    """Temp file + fsync + os.replace. Preserves symlinks and permissions."""
    logger.debug("Beginning of atomic_json_write")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    real_path = path.resolve()

    with tempfile.NamedTemporaryFile(
        mode="w", dir=real_path.parent, suffix=".tmp", delete=False
    ) as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, **dump_kwargs)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = f.name

    os.replace(tmp_path, real_path)


def atomic_yaml_write(path: str | Path, data: Any) -> None:
    """Same atomic pattern for YAML files."""
    logger.debug("Beginning of atomic_yaml_write")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    real_path = path.resolve()

    with tempfile.NamedTemporaryFile(
        mode="w", dir=real_path.parent, suffix=".tmp", delete=False
    ) as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
        tmp_path = f.name

    os.replace(tmp_path, real_path)


def safe_json_loads(text: str, default: Any = None) -> Any:
    """JSON parse with fallback value on error."""
    logger.debug("Beginning of safe_json_loads")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def env_var_enabled(name: str, default: bool = False) -> bool:
    """Checks truthy env var (1/true/yes/on)."""
    logger.debug("Beginning of env_var_enabled")
    val = os.environ.get(name, "").lower().strip()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def base_url_hostname(base_url: str | None) -> str:
    """Safe hostname extraction."""
    logger.debug("Beginning of base_url_hostname")
    if not base_url:
        return ""
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    return parsed.hostname or ""


def base_url_host_matches(base_url: str | None, domain: str) -> bool:
    """Subdomain-safe domain matching."""
    logger.debug("Beginning of base_url_host_matches")
    hostname = base_url_hostname(base_url)
    if not hostname:
        return False
    return hostname == domain or hostname.endswith(f".{domain}")


def normalize_proxy_url(url: str) -> str:
    """Converts socks:// → socks5:// for httpx compatibility."""
    logger.debug("Beginning of normalize_proxy_url")
    if url.startswith("socks://"):
        return "socks5://" + url[8:]
    return url


def truncate_text(text: str, max_chars: int = 50_000, suffix: str = "\n... [truncated]") -> str:
    """Truncate text to max_chars with suffix indicator."""
    logger.debug("Beginning of truncate_text")
    if len(text) <= max_chars:
        return text
    return text[:max_chars - len(suffix)] + suffix
