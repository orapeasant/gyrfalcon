"""Scrub credentials from text on its way out to a chat platform.

A message posted to Slack is stored by Slack, indexed, visible to every member
of the channel and often retained after deletion. An error string or a file the
model read that happens to contain a token is therefore a *permanent* leak once
sent. This is the last line of defence — `read_file` already refuses credential
files for restricted agents — and it is deliberately blunt: a false positive
costs a `[redacted]`, a miss costs a rotated key.
"""

from __future__ import annotations

import re
from typing import Iterable

REDACTED = "[redacted]"

_PATTERNS = [
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),                     # Slack bot/user/etc. tokens
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),                            # Slack app-level tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),                           # OpenAI / Anthropic style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                      # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                              # AWS access key ids
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),            # Authorization headers
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

#: Exact secrets shorter than this are not scrubbed by value: replacing a 3-char
#: string would mangle ordinary text far more often than it would protect anything.
_MIN_SECRET_LENGTH = 8


def redact_secrets(text: str, known: Iterable[str] = ()) -> str:
    """`text` with credential-shaped strings, and any `known` secret values, replaced."""
    if not text:
        return text
    for secret in known:
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
