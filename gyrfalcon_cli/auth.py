"""CLI auth command — manage authentication and credentials."""

from __future__ import annotations

import json
from pathlib import Path

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.config import cfg_get, cfg_set
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("auth")



def run_auth_cli(args: list[str]) -> None:
    """Handle `gyrfalcon auth` subcommands."""
    logger.debug("Beginning of run_auth_cli")
    if not args:
        _show_auth_status()
        return

    subcmd = args[0]
    if subcmd == "login":
        _auth_login(args[1:])
    elif subcmd == "logout":
        _auth_logout()
    elif subcmd == "status":
        _show_auth_status()
    elif subcmd == "switch":
        _auth_switch(args[1:])
    elif subcmd == "list":
        _auth_list()
    else:
        print(f"Unknown auth command: {subcmd}")
        print("Usage: gyrfalcon auth [login|logout|status|switch|list]")


def _auth_login(args: list[str]) -> None:
    """Initiate login flow."""
    logger.debug("Beginning of _auth_login")
    provider = args[0] if args else cfg_get("provider.active", "copilot")

    if provider == "copilot":
        from gyrfalcon.providers.copilot import device_code_flow
        print("Starting GitHub Copilot device auth flow...")
        try:
            import asyncio
            token = asyncio.run(device_code_flow())
            if token:
                _save_credential(provider, {"token": token})
                print("✓ Authenticated with GitHub Copilot")
            else:
                print("✗ Authentication failed")
        except Exception as e:
            print(f"✗ Error: {e}")
    elif provider in ("openai", "anthropic"):
        api_key = input(f"Enter {provider} API key: ").strip()
        if api_key:
            _save_credential(provider, {"api_key": api_key})
            cfg_set("provider.active", provider)
            print(f"✓ Saved {provider} credentials")
        else:
            print("✗ No key provided")
    elif provider == "bedrock":
        print("AWS Bedrock uses AWS credentials from environment or ~/.aws/credentials")
        region = input("AWS region [us-east-1]: ").strip() or "us-east-1"
        _save_credential(provider, {"region": region})
        cfg_set("provider.active", provider)
        print(f"✓ Configured Bedrock (region: {region})")
    else:
        print(f"Unknown provider: {provider}")


def _auth_logout() -> None:
    """Clear stored credentials."""
    logger.debug("Beginning of _auth_logout")
    auth_path = get_gyrfalcon_home() / "auth.json"
    if auth_path.exists():
        auth_path.unlink()
        print("✓ Credentials cleared")
    else:
        print("No credentials stored")


def _show_auth_status() -> None:
    """Show current auth status."""
    logger.debug("Beginning of _show_auth_status")
    auth_path = get_gyrfalcon_home() / "auth.json"
    active = cfg_get("provider.active", "copilot")
    print(f"Active provider: {active}")

    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text())
            for provider, creds in data.items():
                masked = {}
                for k, v in creds.items():
                    if isinstance(v, str) and len(v) > 8:
                        masked[k] = v[:8] + "..."
                    else:
                        masked[k] = "***"
                print(f"  {provider}: {masked}")
        except (json.JSONDecodeError, OSError):
            print("  (credential file corrupt)")
    else:
        print("  No credentials stored")


def _auth_switch(args: list[str]) -> None:
    """Switch active provider."""
    logger.debug("Beginning of _auth_switch")
    if not args:
        print("Usage: gyrfalcon auth switch <provider>")
        return
    provider = args[0]
    cfg_set("provider.active", provider)
    print(f"✓ Switched to provider: {provider}")


def _auth_list() -> None:
    """List configured providers."""
    logger.debug("Beginning of _auth_list")
    auth_path = get_gyrfalcon_home() / "auth.json"
    if not auth_path.exists():
        print("No providers configured. Run: gyrfalcon auth login <provider>")
        return
    try:
        data = json.loads(auth_path.read_text())
        active = cfg_get("provider.active", "copilot")
        print("Configured providers:")
        for provider in data:
            marker = " (active)" if provider == active else ""
            print(f"  • {provider}{marker}")
    except (json.JSONDecodeError, OSError):
        print("Error reading credentials")


def _save_credential(provider: str, creds: dict[str, str]) -> None:
    """Save credentials to auth.json."""
    logger.debug("Beginning of _save_credential")
    auth_path = get_gyrfalcon_home() / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    existing[provider] = creds
    auth_path.write_text(json.dumps(existing, indent=2))
    auth_path.chmod(0o600)
