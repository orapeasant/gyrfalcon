"""Administration > Security: Service Accounts and Secret Store.

Two independent, deliberately small pieces:

- **Service Accounts** are OAuth2 client-credentials clients for *other*
  applications calling *into* Gyrfalcon's own REST API — `client_id` /
  `client_secret` in, a bearer access token out, that `web_server.py`'s
  `_verify_token` accepts alongside the existing dashboard session token.
- **Secret Store** holds secrets Gyrfalcon itself uses to call *out* to other
  systems — a name/value pair a skill or tool can look up by name via
  `get_stored_secret()`. Wiring individual tools to actually use it is a
  separate, later integration; this module only owns storage and lookup.

Both are JSON files under `get_security_dir()` (`<GYRFALCON_HOME>/security/`)
— the same per-profile, plaintext-JSON convention `agents.py`/applications
already use for local, single-operator config. That is the trust boundary:
anyone who can read Gyrfalcon Home can already read `.env` and `auth.json`
sitting right next to it, so a secret's value is stored in the clear here
too, exactly like those files, and the only thing hashed is the OAuth
client secret one can *authenticate* with (§ below).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import time
from typing import Optional

# ── Service accounts ─────────────────────────────────────────────────────────


def _hash_secret(secret: str) -> str:
    """One-way, unsalted SHA-256. Client secrets are high-entropy random
    tokens generated here (never user-chosen), so there is no low-entropy
    guessing risk a salt would defend against — this only has to stop a
    stolen copy of `service_accounts.json` from handing out live secrets."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def load_service_accounts() -> list[dict]:
    from gyrfalcon.gyrfalcon_constants import get_service_accounts_file

    p = get_service_accounts_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("service_accounts", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_service_accounts(accounts: list[dict]) -> None:
    from gyrfalcon.gyrfalcon_constants import get_service_accounts_file

    p = get_service_accounts_file()
    p.write_text(json.dumps({"service_accounts": accounts}, indent=2), encoding="utf-8")


def public_service_account(a: dict) -> dict:
    """Strips the secret hash — what the list/detail endpoints return."""
    return {k: v for k, v in a.items() if k != "client_secret_hash"}


def create_service_account(name: str, scopes: list[str]) -> tuple[dict, str]:
    """Returns (public record, plaintext client_secret). The secret is
    returned only this once — it is never recoverable from storage again,
    only rotated."""
    from gyrfalcon.identity import require_principal

    owner = require_principal()
    accounts = load_service_accounts()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    client_id = f"sa_{_secrets.token_hex(8)}"
    client_secret = _secrets.token_urlsafe(32)
    entry = {
        "id": _secrets.token_hex(16),
        "name": name,
        "client_id": client_id,
        "client_secret_hash": _hash_secret(client_secret),
        "scopes": scopes,
        "tenant_id": owner.tenant_id,
        "created_by": owner.user_id,
        "enabled": True,
        "created_at": now,
        "last_used_at": None,
    }
    accounts.append(entry)
    _save_service_accounts(accounts)
    return public_service_account(entry), client_secret


def rotate_service_account_secret(account_id: str) -> Optional[str]:
    """Returns the new plaintext secret, or None if the account doesn't exist."""
    accounts = load_service_accounts()
    for a in accounts:
        if a["id"] == account_id:
            client_secret = _secrets.token_urlsafe(32)
            a["client_secret_hash"] = _hash_secret(client_secret)
            _save_service_accounts(accounts)
            return client_secret
    return None


def set_service_account_enabled(account_id: str, enabled: bool) -> Optional[dict]:
    accounts = load_service_accounts()
    for a in accounts:
        if a["id"] == account_id:
            a["enabled"] = enabled
            _save_service_accounts(accounts)
            return public_service_account(a)
    return None


def delete_service_account(account_id: str) -> bool:
    accounts = load_service_accounts()
    remaining = [a for a in accounts if a["id"] != account_id]
    if len(remaining) == len(accounts):
        return False
    _save_service_accounts(remaining)
    return True


def authenticate_service_account(client_id: str, client_secret: str) -> Optional[dict]:
    """Validates a client-credentials pair. Returns the account record (with
    hash, for internal use only — do not leak this to a response) or None."""
    accounts = load_service_accounts()
    for a in accounts:
        if a["client_id"] != client_id or not a["enabled"]:
            continue
        if _secrets.compare_digest(a["client_secret_hash"], _hash_secret(client_secret)):
            a["last_used_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_service_accounts(accounts)
            return a
    return None


# ── OAuth2 client-credentials tokens ─────────────────────────────────────────
#
# Opaque bearer tokens held in memory only: they are a per-process cache of
# "this token was legitimately issued", not a record anything else needs to
# survive a restart — a restarted server just means every holder does another
# client-credentials exchange, the same as a normal OAuth token expiring.

_ACCESS_TOKEN_TTL_SECONDS = 3600
_issued_tokens: dict[str, dict] = {}


def issue_access_token(client_id: str, client_secret: str) -> Optional[dict]:
    """OAuth2 client-credentials grant. Returns a token response dict, or
    None if the credentials are invalid/disabled."""
    account = authenticate_service_account(client_id, client_secret)
    if account is None:
        return None
    token = _secrets.token_urlsafe(32)
    expires_at = time.time() + _ACCESS_TOKEN_TTL_SECONDS
    _issued_tokens[token] = {
        "service_account_id": account["id"],
        "client_id": client_id,
        "scopes": account["scopes"],
        "expires_at": expires_at,
    }
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
        "scope": " ".join(account["scopes"]),
    }


def validate_access_token(token: str) -> Optional[dict]:
    """Returns the token's claims if it's live, evicting it (and any other
    now-expired tokens found along the way) if not."""
    claims = _issued_tokens.get(token)
    if claims is None:
        return None
    if claims["expires_at"] < time.time():
        _issued_tokens.pop(token, None)
        return None
    # An access token loses authority when its service account is disabled or
    # removed. Resolve the tenant from the account, not from a shared local
    # principal or client-supplied token claims.
    account = next((a for a in load_service_accounts()
                    if a["id"] == claims["service_account_id"] and a.get("enabled")), None)
    if account is None:
        _issued_tokens.pop(token, None)
        return None
    return {**claims, "tenant_id": account.get("tenant_id", "local"),
            "service_account_name": account.get("name", "")}


# ── Secret store ─────────────────────────────────────────────────────────────


def load_secrets() -> list[dict]:
    from gyrfalcon.gyrfalcon_constants import get_secrets_store_file

    p = get_secrets_store_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("secrets", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_secrets(entries: list[dict]) -> None:
    from gyrfalcon.gyrfalcon_constants import get_secrets_store_file

    p = get_secrets_store_file()
    # Owner-only. This file holds values other systems accept as-is — a Slack
    # bot token is write access to a whole workspace — and a default-umask
    # write leaves it readable by every account on the machine. Created private
    # rather than chmod-ed after, so there is no window; and chmod-ed anyway,
    # because a file that already exists keeps the mode it was created with.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"secrets": entries}, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:  # a filesystem without POSIX modes (some Windows setups)
        pass


def public_secret(s: dict) -> dict:
    """Strips the value — what the list/detail endpoints return. The value
    is write-only from the API's point of view; `get_stored_secret()` below
    is the only reader, for server-side code, never a response body."""
    return {k: v for k, v in s.items() if k != "value"}


def create_secret(name: str, description: str, value: str) -> dict:
    entries = load_secrets()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {
        "id": _secrets.token_hex(16),
        "name": name,
        "description": description,
        "value": value,
        "created_at": now,
        "updated_at": now,
    }
    entries.append(entry)
    _save_secrets(entries)
    return public_secret(entry)


def update_secret(
    secret_id: str, description: Optional[str] = None, value: Optional[str] = None
) -> Optional[dict]:
    entries = load_secrets()
    for s in entries:
        if s["id"] == secret_id:
            if description is not None:
                s["description"] = description
            if value is not None:
                s["value"] = value
            s["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_secrets(entries)
            return public_secret(s)
    return None


def delete_secret(secret_id: str) -> bool:
    entries = load_secrets()
    remaining = [s for s in entries if s["id"] != secret_id]
    if len(remaining) == len(entries):
        return False
    _save_secrets(remaining)
    return True


def get_stored_secret(name: str) -> Optional[str]:
    """Server-side lookup by name, for a tool/skill that needs to call out to
    another system. Not exposed over the REST API — only the value's owner
    (code running as Gyrfalcon itself) reads it."""
    for s in load_secrets():
        if s["name"] == name:
            return s.get("value")
    return None
