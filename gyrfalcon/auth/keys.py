"""API keys for non-interactive callers.

Spec: §17.3 — the REST gateway's `gyr_live_…` keys become *user*-scoped rather
than server-scoped, so a service call is attributable to a person and confined
to their tenant.

Two properties matter more than the format:

* **Only a hash is stored.** A leaked database must not yield working keys.
* **Comparison is constant-time.** Comparing hashes with `==` leaks their
  contents through timing, one byte at a time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

PREFIX = "gyr_live_"
#: Length of the non-secret lookup handle. Long enough to be unique in
#: practice, short enough that it is worthless on its own.
PREFIX_LEN = 12


def mint_key() -> tuple[str, str, str]:
    """Return `(key, prefix, key_hash)`.

    The full key is returned exactly once, at creation, and is never
    recoverable afterwards — that is the point of storing only the hash.
    """
    secret = secrets.token_urlsafe(32)
    key = f"{PREFIX}{secret}"
    return key, key[: len(PREFIX) + PREFIX_LEN], hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def split_key(key: str) -> tuple[str, str] | tuple[None, None]:
    """`(prefix, hash)` for a presented key, or `(None, None)` if malformed.

    Malformed is not an error worth raising: it arrives from the network, and
    a caller sending nonsense should get "unauthenticated", not a stack trace.
    """
    if not key or not key.startswith(PREFIX) or len(key) <= len(PREFIX) + PREFIX_LEN:
        return None, None
    return key[: len(PREFIX) + PREFIX_LEN], hash_key(key)


def matches(presented_hash: str, stored_hash: str) -> bool:
    """Constant-time comparison of two hex digests."""
    return hmac.compare_digest(presented_hash, stored_hash)


def format_key(key: str) -> str:
    """A display form safe to log or show in a list — never the whole key."""
    if not key:
        return ""
    return f"{key[: len(PREFIX) + 4]}…{key[-4:]}"
