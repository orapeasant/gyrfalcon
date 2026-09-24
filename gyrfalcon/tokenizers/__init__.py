"""Token counting, per model family.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.5.1.

> **On the package name.** This is `gyrfalcon.tokenizers`; the Hugging Face
> library is the top-level `tokenizers`. Python 3 has no implicit relative
> imports, so `import tokenizers` inside this package resolves to the installed
> library, not to this one.
"""

from gyrfalcon.tokenizers.registry import (
    API,
    APPROX,
    EXACT,
    UNAVAILABLE,
    TokenCount,
    count_request,
    count_text,
    describe,
    is_available,
)

__all__ = [
    "API",
    "APPROX",
    "EXACT",
    "UNAVAILABLE",
    "TokenCount",
    "count_request",
    "count_text",
    "describe",
    "is_available",
]
