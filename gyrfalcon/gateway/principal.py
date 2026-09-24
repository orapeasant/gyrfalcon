"""Who a chat message is from, as far as the rest of the platform is concerned.

Spec 18-slack.md D4. With `identity.enabled` off — the default, and what a
`RUN_MODE=CLIENT` install is — everything is the `LOCAL` principal and this is a
one-liner. The seam exists so that turning identity on later is a linking flow
and a lookup here, not a rewrite of the message path.

With identity on there is no permissive fallback (§17): an unresolvable sender is
*refused*, never quietly treated as `LOCAL`, because that would attribute a
stranger's messages to the install's owner. Until the linking flow exists,
turning identity on therefore makes the gateway refuse every chat message — a
loud, safe failure rather than a silent wrong one.
"""

from __future__ import annotations

from gyrfalcon.gateway.platforms.base import SessionSource
from gyrfalcon.identity import LOCAL, Principal, identity_enabled


class UnlinkedIdentityError(RuntimeError):
    """The sender cannot be mapped to a Gyrfalcon user."""


def resolve_principal(source: SessionSource) -> Principal:
    if not identity_enabled():
        return LOCAL
    raise UnlinkedIdentityError(
        f"Identity is enabled but {source.platform} user {source.user_id!r} is not linked to a "
        "Gyrfalcon account, and account linking for chat platforms is not implemented yet."
    )
