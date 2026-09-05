"""Setup wizard — interactive first-run configuration."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from gyrfalcon.config import save_config, load_config, save_env_value, get_env_value, DEFAULT_CONFIG
from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_app_name
from gyrfalcon.providers.copilot import device_code_flow, is_authenticated
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("setup")



def _get_current_provider() -> str | None:
    """Detect which provider is currently configured."""
    logger.debug("Beginning of _get_current_provider")
    if is_authenticated():
        return "copilot"
    if get_env_value("OPENAI_API_KEY"):
        return "openai"
    if get_env_value("ANTHROPIC_API_KEY"):
        return "anthropic"
    if get_env_value("OPENROUTER_API_KEY"):
        return "openrouter"
    if get_env_value("AWS_ACCESS_KEY_ID"):
        return "bedrock"
    return None


def run_setup():
    """Interactive setup wizard."""
    logger.debug("Beginning of run_setup")
    console = Console()

    console.print(Panel(f"[bold]{get_app_name()} Setup Wizard[/bold]", border_style="bright_yellow"))
    console.print()

    config = load_config()

    # Step 1: Choose provider
    _setup_provider(console, config)

    # Step 2: Agent settings
    _setup_agent(console, config)

    # Step 3: Security
    _setup_security(console, config)

    # Save
    save_config(config)
    console.print(f"\n[green]✓ Configuration saved to {get_gyrfalcon_home() / 'config.yaml'}[/green]")
    console.print("\nRun `gyrfalcon` to start chatting!")


def _setup_provider(console: Console, config: dict) -> None:
    """Step 1: Provider selection."""
    logger.debug("Beginning of _setup_provider")
    console.print("[bold]Step 1: Choose your LLM provider[/bold]\n")

    current = _get_current_provider()
    current_model = config.get("model", {}).get("name", "")

    provider_map = {
        "1": ("copilot", "GitHub Copilot", "Free with GitHub Copilot subscription (device auth)"),
        "2": ("openai", "OpenAI", "Direct OpenAI API (requires API key)"),
        "3": ("anthropic", "Anthropic", "Direct Anthropic API (requires API key)"),
        "4": ("openrouter", "OpenRouter", "Multi-model router (requires API key)"),
        "5": ("bedrock", "AWS Bedrock", "AWS managed models (requires AWS credentials)"),
    }

    choices = ["1", "2", "3", "4", "5"]

    # Show "keep current" option if a provider is already configured
    if current:
        current_display = next(
            (name for _, (key, name, _) in provider_map.items() if key == current),
            current,
        )
        console.print(f"  [0] Keep current setting [dim]({current_display}, model: {current_model})[/dim]")
        choices.insert(0, "0")

    for num, (_, name, desc) in provider_map.items():
        console.print(f"  [{num}] {name} — {desc}")

    default = "0" if current else "1"
    choice = Prompt.ask("\nSelect provider", choices=choices, default=default)

    if choice == "0":
        console.print("[dim]  Keeping current provider settings.[/dim]")
        return

    provider_name = None

    if choice == "1":
        provider_name = "copilot"
        if is_authenticated():
            console.print("\n[green]✓ Already authenticated with GitHub Copilot[/green]")
            if not Confirm.ask("  Re-authenticate?", default=False):
                _select_model_for_provider(console, config, provider_name)
                return
        console.print("\nAuthenticating with GitHub Copilot...")
        token = device_code_flow(print_fn=console.print)
        if not token:
            console.print("[red]Authentication failed. You can retry with `gyrfalcon setup`[/red]")
            return

    elif choice == "2":
        provider_name = "openai"
        key = Prompt.ask("OpenAI API key", password=True)
        save_env_value("OPENAI_API_KEY", key)

    elif choice == "3":
        provider_name = "anthropic"
        key = Prompt.ask("Anthropic API key", password=True)
        save_env_value("ANTHROPIC_API_KEY", key)

    elif choice == "4":
        provider_name = "openrouter"
        key = Prompt.ask("OpenRouter API key", password=True)
        save_env_value("OPENROUTER_API_KEY", key)

    elif choice == "5":
        provider_name = "bedrock"
        region = Prompt.ask("AWS Region", default="us-east-1")
        save_env_value("AWS_DEFAULT_REGION", region)
        if Confirm.ask("Configure AWS credentials now?"):
            access_key = Prompt.ask("AWS Access Key ID")
            secret_key = Prompt.ask("AWS Secret Access Key", password=True)
            save_env_value("AWS_ACCESS_KEY_ID", access_key)
            save_env_value("AWS_SECRET_ACCESS_KEY", secret_key)

    # Model selection for the chosen provider
    if provider_name:
        _select_model_for_provider(console, config, provider_name)


def _select_model_for_provider(console: Console, config: dict, provider_name: str) -> None:
    """Interactive model selection for a given provider."""
    logger.debug("Beginning of _select_model_for_provider")
    from gyrfalcon.providers import get_provider_profile

    profile = get_provider_profile(provider_name)
    if not profile:
        # Fallback to free-text input
        model = Prompt.ask("Model name", default=config.get("model", {}).get("name", ""))
        config["model"]["name"] = model
        return

    models = profile.fetch_models()
    if not models:
        model = Prompt.ask("Model name", default=profile.default_model or "")
        config["model"]["name"] = model
        return

    current_model = config.get("model", {}).get("name", "")
    default_model = profile.default_model or models[0]

    console.print(f"\n[bold]Select model for {profile.display_name}:[/bold]\n")

    for i, model_name in enumerate(models, 1):
        marker = " [green](current)[/green]" if model_name == current_model else ""
        default_marker = " [dim](default)[/dim]" if model_name == default_model and model_name != current_model else ""
        console.print(f"  [{i}] {model_name}{marker}{default_marker}")

    # Determine default choice
    if current_model in models:
        default_idx = str(models.index(current_model) + 1)
    else:
        default_idx = str(models.index(default_model) + 1) if default_model in models else "1"

    valid_choices = [str(i) for i in range(1, len(models) + 1)]
    choice = Prompt.ask("\nSelect model", choices=valid_choices, default=default_idx)

    selected = models[int(choice) - 1]
    config["model"]["name"] = selected
    console.print(f"[green]✓ Model set to: {selected}[/green]")


def _setup_agent(console: Console, config: dict) -> None:
    """Step 2: Agent settings."""
    logger.debug("Beginning of _setup_agent")
    console.print("\n[bold]Step 2: Agent settings[/bold]\n")

    current_max_turns = config.get("agent", {}).get("max_turns", 90)

    console.print(f"  [0] Keep current setting [dim](max_turns: {current_max_turns})[/dim]")
    console.print(f"  [1] Change max tool-calling iterations")

    choice = Prompt.ask("Select", choices=["0", "1"], default="0")
    if choice == "0":
        console.print("[dim]  Keeping current agent settings.[/dim]")
        return

    max_turns = Prompt.ask("Max tool-calling iterations", default=str(current_max_turns))
    config["agent"]["max_turns"] = int(max_turns)


def _setup_security(console: Console, config: dict) -> None:
    """Step 3: Security settings."""
    logger.debug("Beginning of _setup_security")
    console.print("\n[bold]Step 3: Security[/bold]\n")

    current_approval = config.get("security", {}).get("approval_mode", "smart")

    console.print(f"  [0] Keep current setting [dim](approval: {current_approval})[/dim]")
    console.print("  [1] smart  — Ask for approval on dangerous commands")
    console.print("  [2] always — Always ask before executing commands")
    console.print("  [3] none   — Never ask (use with caution)")

    choice = Prompt.ask("Select", choices=["0", "1", "2", "3"], default="0")

    if choice == "0":
        console.print("[dim]  Keeping current security settings.[/dim]")
        return

    mode_map = {"1": "smart", "2": "always", "3": "none"}
    config["security"]["approval_mode"] = mode_map[choice]
