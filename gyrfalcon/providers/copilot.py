"""GitHub Copilot provider with device authentication flow."""

from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
from typing import Optional

import httpx

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.net import httpx_request
from gyrfalcon.providers import ProviderProfile, register_provider

logger = get_logger("providers.copilot")

COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_CHAT_URL = "https://api.githubcopilot.com"

_token_cache: dict[str, str] = {}
_token_expiry: float = 0.0
_token_lock = threading.Lock()


class CopilotProvider(ProviderProfile):
    """GitHub Copilot provider with device code authentication."""

    def __init__(self):
        super().__init__(
            name="copilot",
            api_mode="chat_completions",
            aliases=["github-copilot", "gh-copilot"],
            display_name="GitHub Copilot",
            env_vars=[],
            base_url=COPILOT_CHAT_URL,
            auth_type="copilot",
            fallback_models=["gpt-4o", "gpt-5-mini", "claude-opus-4.5", "gemini-2.5-pro"],
            hostname="api.githubcopilot.com",
            default_model="gpt-4o",
        )

    def fetch_models(self) -> list[str]:
        """Fetch available models from the Copilot API."""
        logger.debug("Beginning of fetch_models")
        try:
            token = get_copilot_token()
            if not token:
                return self.fallback_models

            response = httpx_request(
                "GET",
                f"{COPILOT_CHAT_URL}/models",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Copilot-Integration-Id": "vscode-chat",
                    "Editor-Version": "vscode/1.96.0",
                    "Editor-Plugin-Version": "copilot-chat/0.24.0",
                },
                timeout=10.0,
            )
            if response.status_code != 200:
                return self.fallback_models

            data = response.json()
            models = []
            for m in data.get("data", []):
                caps = m.get("capabilities", {})
                if caps.get("type") == "chat" and m.get("model_picker_enabled"):
                    models.append(m["id"])

            return models if models else self.fallback_models
        except Exception as e:
            logger.warning(f"Failed to fetch Copilot models: {e}")
            return self.fallback_models

    def build_api_kwargs_extras(self, **kwargs) -> dict:
        logger.debug("Beginning of build_api_kwargs_extras")
        return {
            "extra_headers": {
                "Copilot-Integration-Id": "vscode-chat",
                "Editor-Version": "vscode/1.96.0",
                "Editor-Plugin-Version": "copilot-chat/0.24.0",
            }
        }


def get_auth_file() -> Path:
    """Path to stored auth credentials."""
    logger.debug("Beginning of get_auth_file")
    return get_gyrfalcon_home() / "auth.json"


def _load_auth() -> dict:
    """Load stored auth data."""
    logger.debug("Beginning of _load_auth")
    auth_file = get_auth_file()
    if auth_file.exists():
        try:
            return json.loads(auth_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_auth(data: dict) -> None:
    """Save auth data."""
    logger.debug("Beginning of _save_auth")
    auth_file = get_auth_file()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps(data, indent=2))
    auth_file.chmod(0o600)


def device_code_flow(print_fn=print) -> Optional[str]:
    """Run GitHub device code OAuth flow. Returns OAuth token."""
    logger.debug("Beginning of device_code_flow")
    print_fn("Starting GitHub Copilot device authentication...")

    # Step 1: Request device code
    response = httpx_request(
        "POST",
        GITHUB_DEVICE_CODE_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": COPILOT_CLIENT_ID,
            "scope": "read:user",
        },
    )
    response.raise_for_status()
    data = response.json()

    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data["verification_uri"]
    interval = data.get("interval", 5)
    expires_in = data.get("expires_in", 900)

    print_fn(f"\n  Open: {verification_uri}")
    print_fn(f"  Enter code: {user_code}\n")
    print_fn("Waiting for authorization...")

    # Step 2: Poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        token_response = httpx_request(
            "POST",
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": COPILOT_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        token_data = token_response.json()

        if "access_token" in token_data:
            oauth_token = token_data["access_token"]
            # Save token
            auth = _load_auth()
            auth["copilot"] = {
                "oauth_token": oauth_token,
                "token_type": token_data.get("token_type", "bearer"),
                "scope": token_data.get("scope", ""),
                "created_at": time.time(),
            }
            _save_auth(auth)
            print_fn("✓ GitHub Copilot authenticated successfully!")
            return oauth_token

        error = token_data.get("error", "")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            interval += 5
        elif error in ("expired_token", "access_denied"):
            print_fn(f"Authentication failed: {error}")
            return None

    print_fn("Authentication timed out.")
    return None


def get_copilot_token() -> Optional[str]:
    """Get a valid Copilot API token. Handles token exchange and caching.

    Flow:
    1. Try cached short-lived token
    2. Exchange OAuth token for a short-lived Copilot token
    3. If exchange fails, use the OAuth token directly (it works as Bearer)
    """
    logger.debug("Beginning of get_copilot_token")
    global _token_cache, _token_expiry

    with _token_lock:
        # Check cache
        if _token_cache.get("token") and time.time() < _token_expiry - 60:
            return _token_cache["token"]

    # Get OAuth token
    auth = _load_auth()
    copilot_auth = auth.get("copilot", {})
    oauth_token = copilot_auth.get("oauth_token")

    if not oauth_token:
        # Check environment
        oauth_token = os.environ.get("GITHUB_TOKEN")
        if not oauth_token:
            return None

    # Try exchanging for a short-lived Copilot token
    try:
        response = httpx_request(
            "GET",
            COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {oauth_token}",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.96.0",
                "Editor-Plugin-Version": "copilot-chat/0.24.0",
                "Copilot-Integration-Id": "vscode-chat",
                "User-Agent": "GitHubCopilotChat/0.24.0",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

        token = data.get("token")
        expires_at = data.get("expires_at", time.time() + 1800)

        with _token_lock:
            _token_cache["token"] = token
            _token_expiry = expires_at

        return token
    except Exception as e:
        logger.warning(f"Token exchange failed ({e}), using OAuth token directly")
        # Use the OAuth token directly — Copilot Chat API accepts it as Bearer
        with _token_lock:
            _token_cache["token"] = oauth_token
            _token_expiry = time.time() + 3600

        return oauth_token


def get_copilot_credentials() -> tuple[str, str]:
    """Get (base_url, api_key) for Copilot. Returns empty api_key on failure."""
    logger.debug("Beginning of get_copilot_credentials")
    token = get_copilot_token()
    if token:
        return COPILOT_CHAT_URL, token
    return COPILOT_CHAT_URL, ""


def credentials_valid() -> bool:
    """Check if Copilot credentials can be exchanged for a valid API token."""
    logger.debug("Beginning of credentials_valid")
    token = get_copilot_token()
    return bool(token)


def is_authenticated() -> bool:
    """Check if we have valid Copilot credentials."""
    logger.debug("Beginning of is_authenticated")
    auth = _load_auth()
    return bool(auth.get("copilot", {}).get("oauth_token")) or bool(os.environ.get("GITHUB_TOKEN"))


def logout() -> None:
    """Remove stored Copilot credentials."""
    logger.debug("Beginning of logout")
    auth = _load_auth()
    auth.pop("copilot", None)
    _save_auth(auth)
    global _token_cache, _token_expiry
    with _token_lock:
        _token_cache.clear()
        _token_expiry = 0.0


# Register provider
_copilot_provider = CopilotProvider()
register_provider(_copilot_provider)
