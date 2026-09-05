"""Diagnostics — system health checks."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_config_path, get_env_path, get_app_name
from gyrfalcon.config import load_config, get_env_value
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("doctor")



def run_doctor():
    """Run diagnostics."""
    logger.debug("Beginning of run_doctor")
    console = Console()
    console.print(f"[bold]{get_app_name()} Doctor[/bold]\n")

    table = Table(show_header=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    # Home directory
    home = get_gyrfalcon_home()
    table.add_row("Home directory", "✓", str(home))

    # Config file
    config_path = get_config_path()
    if config_path.exists():
        table.add_row("Config file", "✓", str(config_path))
    else:
        table.add_row("Config file", "⚠", "Not found (using defaults)")

    # Env file
    env_path = get_env_path()
    if env_path.exists():
        table.add_row("Env file", "✓", str(env_path))
    else:
        table.add_row("Env file", "⚠", "Not found")

    # Model configuration
    config = load_config()
    model = config.get("model", {}).get("name", "")
    if model:
        table.add_row("Model", "✓", model)
    else:
        table.add_row("Model", "⚠", "Not configured")

    # API keys
    providers_check = [
        ("OpenAI", "OPENAI_API_KEY"),
        ("Anthropic", "ANTHROPIC_API_KEY"),
        ("OpenRouter", "OPENROUTER_API_KEY"),
    ]
    for name, env_var in providers_check:
        val = get_env_value(env_var)
        if val:
            table.add_row(f"{name} key", "✓", f"{env_var} set")
        else:
            table.add_row(f"{name} key", "—", "Not set")

    # Copilot auth
    from gyrfalcon.providers.copilot import is_authenticated
    if is_authenticated():
        table.add_row("GitHub Copilot", "✓", "Authenticated")
    else:
        table.add_row("GitHub Copilot", "—", "Not authenticated")

    # AWS credentials
    if get_env_value("AWS_ACCESS_KEY_ID"):
        table.add_row("AWS Bedrock", "✓", "Credentials set")
    else:
        table.add_row("AWS Bedrock", "—", "Not configured")

    # Python packages
    for pkg in ["openai", "anthropic", "httpx", "rich", "prompt_toolkit"]:
        try:
            __import__(pkg)
            table.add_row(f"Package: {pkg}", "✓", "Installed")
        except ImportError:
            table.add_row(f"Package: {pkg}", "✗", "Missing")

    # Session DB
    try:
        from gyrfalcon.gyrfalcon_state import SessionDB
        db = SessionDB()
        sessions = db.list_sessions(limit=1)
        db.close()
        table.add_row("Session DB", "✓", "Working")
    except Exception as e:
        table.add_row("Session DB", "✗", str(e))

    console.print(table)
