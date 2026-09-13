"""OpenID Connect login — Authorization Code flow with PKCE.

Spec: §17.11 step 8. Written against Entra, but there is nothing
Entra-specific here beyond the default scopes: everything comes from the
provider's discovery document.

**On signature validation, because the omission is deliberate and the reason
matters.** This module never verifies the ID token's JWT signature. In the
authorization-code flow that is sound, and OIDC Core §3.1.3.7 says so: the
token is fetched by *this server* over TLS directly from the provider's token
endpoint, in a request authenticated by the client credentials and bound to a
`code_verifier` only this server holds. There is no untrusted party in that
path to forge it. What is validated instead are the claims that TLS cannot
speak to — issuer, audience, expiry, and the nonce that ties the token to this
particular login attempt.

That reasoning collapses the moment a token arrives from a *client* rather
than from the provider. So `accept_bearer_token()` exists and refuses, loudly,
rather than leaving a plausible-looking function that would accept anything
handed to it. Non-interactive callers use gyrfalcon's own API keys
(`auth/keys.py`), which need no crypto to verify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

DEFAULT_SCOPES = ("openid", "profile", "email")
#: Discovery documents change rarely; refetching one per login would add a
#: round trip to every sign-in for no benefit.
_DISCOVERY_TTL = 3600.0
_discovery_cache: dict[str, tuple[float, dict]] = {}


class OIDCError(RuntimeError):
    """Login could not be completed. The message is safe to log, not to show."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.redirect_uri)


@dataclass
class LoginAttempt:
    """The server-side half of one login, held until the callback returns.

    `state` defends against CSRF on the callback; `nonce` binds the returned
    ID token to this attempt; `code_verifier` is what makes the authorization
    code useless to anyone who intercepts it (PKCE). All three are generated
    per attempt and never reused.
    """

    state: str
    nonce: str
    code_verifier: str
    created_at: float = field(default_factory=time.time)
    next_url: str = "/"

    def expired(self, max_age: float = 600.0) -> bool:
        return (time.time() - self.created_at) > max_age


def oidc_config() -> OIDCConfig:
    from gyrfalcon.config import cfg_get

    scopes = cfg_get("identity.oidc.scopes", None) or list(DEFAULT_SCOPES)
    return OIDCConfig(
        issuer=str(cfg_get("identity.oidc.issuer", "") or "").rstrip("/"),
        client_id=str(cfg_get("identity.oidc.client_id", "") or ""),
        client_secret=str(cfg_get("identity.oidc.client_secret", "") or ""),
        redirect_uri=str(cfg_get("identity.oidc.redirect_uri", "") or ""),
        scopes=tuple(scopes),
    )


def discover(issuer: str, timeout: float = 10.0) -> dict:
    """Fetch and cache the provider's OpenID configuration."""
    now = time.time()
    cached = _discovery_cache.get(issuer)
    if cached and (now - cached[0]) < _DISCOVERY_TTL:
        return cached[1]

    import httpx

    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        document = response.json()
    except Exception as exc:
        raise OIDCError(f"Could not fetch OIDC discovery document from {url}") from exc
    for required in ("authorization_endpoint", "token_endpoint", "issuer"):
        if required not in document:
            raise OIDCError(f"Discovery document from {url} has no {required!r}")
    _discovery_cache[issuer] = (now, document)
    return document


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def begin_login(config: Optional[OIDCConfig] = None,
                next_url: str = "/") -> tuple[str, LoginAttempt]:
    """Return the URL to send the browser to, and the state to keep."""
    config = config or oidc_config()
    if not config.enabled:
        raise OIDCError(
            "OIDC is not configured. Set identity.oidc.issuer, .client_id and "
            ".redirect_uri."
        )
    document = discover(config.issuer)

    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    attempt = LoginAttempt(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=verifier,
        next_url=next_url,
    )
    query = urlencode({
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(config.scopes),
        "state": attempt.state,
        "nonce": attempt.nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{document['authorization_endpoint']}?{query}", attempt


def exchange_code(code: str, attempt: LoginAttempt,
                  config: Optional[OIDCConfig] = None, timeout: float = 10.0) -> dict:
    """Trade the authorization code for tokens, and return the ID token claims."""
    config = config or oidc_config()
    document = discover(config.issuer)

    import httpx

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "client_id": config.client_id,
        "code_verifier": attempt.code_verifier,
    }
    if config.client_secret:
        form["client_secret"] = config.client_secret
    try:
        response = httpx.post(document["token_endpoint"], data=form, timeout=timeout)
    except Exception as exc:
        raise OIDCError("Token endpoint unreachable") from exc
    if response.status_code != 200:
        # The provider's body can contain the code; keep it out of the message.
        raise OIDCError(f"Token exchange rejected (HTTP {response.status_code})")

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise OIDCError("Token response contained no id_token")
    claims = decode_claims_unverified(id_token)
    validate_claims(claims, attempt, config, document)
    return claims


def decode_claims_unverified(id_token: str) -> dict[str, Any]:
    """Read a JWT payload **without checking its signature**.

    Named to be uncomfortable on purpose. Only safe for a token this server
    just fetched over TLS from the token endpoint; see the module docstring.
    """
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise OIDCError("id_token is not a readable JWT") from exc


def validate_claims(claims: dict, attempt: LoginAttempt, config: OIDCConfig,
                    document: dict, leeway: float = 60.0) -> None:
    """Check what TLS cannot: who issued it, who it is for, and when."""
    issuer = claims.get("iss", "")
    if issuer.rstrip("/") != str(document["issuer"]).rstrip("/"):
        raise OIDCError("id_token issuer does not match the discovery document")

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if config.client_id not in audiences:
        raise OIDCError("id_token was not issued for this client")

    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)) or time.time() > (expiry + leeway):
        raise OIDCError("id_token has expired")

    if claims.get("nonce") != attempt.nonce:
        raise OIDCError("id_token nonce does not match this login attempt")

    if not claims.get("sub"):
        raise OIDCError("id_token has no subject")


def accept_bearer_token(token: str) -> None:
    """Refuse to authenticate a client-presented JWT.

    Deliberately not implemented. Accepting a token from a client requires
    real signature verification against the provider's JWKS, and the
    claim-only checks above are not a substitute — a caller can put anything
    in an unsigned payload. Non-interactive callers use API keys instead.
    """
    raise NotImplementedError(
        "Bearer-token authentication is not implemented: it needs JWKS "
        "signature verification, which this module deliberately does not do. "
        "Use a per-user API key (gyr_live_...) for non-interactive callers."
    )


def identity_from_claims(claims: dict, org_id: Optional[str] = None):
    """Map validated claims onto a gyrfalcon principal.

    The IdP says who someone is; gyrfalcon decides what they belong to. A
    person who authenticates but has no membership gets no principal — logging
    in is not the same as being allowed in.
    """
    from gyrfalcon.auth.store import get_auth_store

    store = get_auth_store()
    user = store.upsert_user(
        issuer=claims["iss"],
        subject=claims["sub"],
        email=claims.get("email") or claims.get("preferred_username") or "",
        display_name=claims.get("name") or "",
    )
    return store.principal_for(user["id"], org_id, source="oidc")
