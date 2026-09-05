"""Path resolution, environment detection, and constants."""

import os
import platform
import functools
from pathlib import Path


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
VALID_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_CONTEXT_LENGTH = 128_000
DEFAULT_MAX_ITERATIONS = 90
DEFAULT_APP_NAME = "Gyrfalcon"


@functools.lru_cache(maxsize=1)
def get_repo_root_env_path() -> Path:
    """Path to the repo-root .env (dev-time config, distinct from the per-profile ~/.gyrfalcon/.env)."""
    return Path(__file__).resolve().parent.parent / ".env"


def load_repo_root_env_file() -> None:
    """Load the repo-root .env into the environment, without overriding already-set vars."""
    env_path = get_repo_root_env_path()
    if not env_path.exists():
        return
    from dotenv import dotenv_values
    for key, value in dotenv_values(str(env_path)).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def get_app_name() -> str:
    """Project display name — configurable via GYRFALCON_APP_NAME in the root .env file."""
    return os.environ.get("GYRFALCON_APP_NAME", DEFAULT_APP_NAME)


def get_org_name() -> str:
    """Organization shown under the user name — GYRFALCON_ORG_NAME in the root .env file.

    Falls back to the app name so the sidebar never renders an empty line.
    """
    return os.environ.get("GYRFALCON_ORG_NAME", "").strip() or get_app_name()


@functools.lru_cache(maxsize=1)
def get_gyrfalcon_home() -> Path:
    """Single source of truth for ~/.gyrfalcon path. Profile-aware via GYRFALCON_HOME env var."""
    home = os.environ.get("GYRFALCON_HOME")
    if home:
        p = Path(home).expanduser().resolve()
    else:
        p = Path.home() / ".gyrfalcon"
    p.mkdir(parents=True, exist_ok=True)
    return p


def display_gyrfalcon_home() -> str:
    """User-friendly ~/relative display path."""
    home = get_gyrfalcon_home()
    try:
        return f"~/{home.relative_to(Path.home())}"
    except ValueError:
        return str(home)


def get_config_path() -> Path:
    return get_gyrfalcon_home() / "config.yaml"


def get_env_path() -> Path:
    return get_gyrfalcon_home() / ".env"


def get_skills_dir() -> Path:
    d = get_gyrfalcon_home() / "skills"
    d.mkdir(exist_ok=True)
    return d


def get_optional_skills_dir() -> Path:
    return Path(__file__).parent.parent / "optional_skills"


def get_logs_dir() -> Path:
    d = get_gyrfalcon_home() / "logs"
    d.mkdir(exist_ok=True)
    return d


def get_plugins_dir() -> Path:
    d = get_gyrfalcon_home() / "plugins"
    d.mkdir(exist_ok=True)
    return d


def get_mcp_dir() -> Path:
    d = get_gyrfalcon_home() / "mcp"
    d.mkdir(exist_ok=True)
    return d


def get_mcp_file() -> Path:
    return get_mcp_dir() / "mcp.json"


def get_applications_file() -> Path:
    return get_gyrfalcon_home() / "applications.json"


def get_agents_file() -> Path:
    return get_gyrfalcon_home() / "agents.json"


def get_skillshub_file() -> Path:
    """Path to ~/.gyrfalcon/skills/skillshub.json — registry config lives here."""
    skills_dir = get_skills_dir()  # already creates the dir
    return skills_dir / "skillshub.json"


@functools.lru_cache(maxsize=1)
def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "")


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except (OSError, PermissionError):
        return False


@functools.lru_cache(maxsize=1)
def is_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        return "docker" in cgroup or "containerd" in cgroup
    except (OSError, PermissionError):
        return False


def parse_reasoning_effort(effort: str) -> tuple[bool, str]:
    """Validates reasoning effort. Returns (enabled, effort_level)."""
    effort = effort.lower().strip()
    if effort not in VALID_REASONING_EFFORTS:
        raise ValueError(f"Invalid reasoning effort: {effort}. Valid: {VALID_REASONING_EFFORTS}")
    if effort == "none":
        return False, effort
    return True, effort
