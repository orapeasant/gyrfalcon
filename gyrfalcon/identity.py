"""Who is acting — the identity every tenant-scoped decision hangs off.

Spec: 15-flow.md §17.3.

Deliberately at the top level rather than inside `flow/`: sessions, the
gateway, the REST gateway, and the dashboard all need the same answer to "who
is this", and a second notion of identity living beside the first is how two
subsystems end up disagreeing about who owns a row.

This module is the *representation and propagation* of identity. It does not
authenticate anyone — verifying a bearer token or an API key belongs to the
edge that receives the request, which then calls `use_principal()`.

The one rule worth stating loudly, because getting it wrong is silent:

    **A lost principal is never a permissive default.**

Identity rides a `contextvars.ContextVar`, and this codebase has already been
bitten twice by contextvars not crossing a thread boundary (`futures.py`'s
task pool, `engine.py`'s timeout worker). Those bugs cost a parent link. The
same bug here costs tenant isolation: code that "falls back" when identity is
missing would read or write another tenant's data. So `require_principal()`
raises, and there is no ambient super-user to fall back to.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Iterator, Optional


class MissingPrincipalError(RuntimeError):
    """Identity was required and absent. Never downgrade this to a default."""


@dataclass(frozen=True)
class Principal:
    """One acting identity: a person, a service key, or the platform itself.

    `user_id` and `tenant_id` are the two keys every scoped query filters on.
    Both are opaque and stable — an email address is *not* a user id, because
    people's addresses change and rows would be orphaned when they do.
    """

    user_id: str
    tenant_id: str = ""
    display_name: str = ""
    email: str = ""
    #: Authorization groups, normally mapped from the IdP's group claims
    #: rather than maintained by hand (§17.6).
    roles: frozenset[str] = field(default_factory=frozenset)
    #: How this identity was established: "oidc", "api_key", "local", "system".
    source: str = "unknown"

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ValueError("Principal.user_id must be non-empty")
        # A tenant is required for scoping; default it to the user's own id so
        # a personal install is a tenant of one rather than a special case
        # every query has to remember.
        if not self.tenant_id:
            object.__setattr__(self, "tenant_id", self.user_id)
        if not isinstance(self.roles, frozenset):
            object.__setattr__(self, "roles", frozenset(self.roles))

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def is_operator(self) -> bool:
        """May see and act on the whole tenant, not just its own rows (§17.6)."""
        return self.has_role(ROLE_OPERATOR, ROLE_ADMIN)


ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"

#: The implicit identity of a single-user install. Not a bypass: rows are
#: genuinely owned by "local" and filter like anyone else's, which is what
#: makes a later migration to real accounts a data change rather than a
#: semantics change.
LOCAL = Principal(user_id="local", tenant_id="local", display_name="Local",
                  roles=frozenset({ROLE_OPERATOR}), source="local")

#: The platform acting on its own behalf — crash reconciliation, migrations,
#: schema maintenance. Named rather than blank so an audit trail can say what
#: touched a row. It is *not* a way to read across tenants: cross-tenant reads
#: go through an explicit scope (§17.5), and work performed on a user's behalf
#: must run as that user, not as this.
SYSTEM = Principal(user_id="system", tenant_id="system", display_name="System",
                   roles=frozenset({ROLE_ADMIN}), source="system")


def owner_of(row) -> Principal:
    """Reconstruct the owning identity of a stored row.

    Carries **no roles**, deliberately. Roles are an authorization fact
    established when someone authenticates against the IdP; reading them back
    off a row would mean anyone able to write a row could grant themselves
    privileges. This principal is for *attribution and scoping* — running a
    deployment's work under the person who created it — never for deciding
    what that work is allowed to do.
    """
    user_id = row["user_id"]
    return Principal(
        user_id=user_id,
        tenant_id=row["tenant_id"] or user_id,
        source="stored",
    )


_PRINCIPAL: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "gyrfalcon_principal", default=None
)


def get_principal() -> Optional[Principal]:
    """The current identity, or None. Prefer `require_principal()` for anything
    that reads or writes tenant-scoped data."""
    return _PRINCIPAL.get()


def require_principal() -> Principal:
    """The current identity, raising if there is none.

    In single-user mode (`identity.enabled` false) this resolves to `LOCAL`,
    which is a real filtered identity rather than an escape hatch. With
    identity enabled, absence is an error — because the alternative is
    attributing one user's action to another, or to nobody.
    """
    principal = _PRINCIPAL.get()
    if principal is not None:
        return principal
    if identity_enabled():
        raise MissingPrincipalError(
            "No principal in context. Identity is enabled, so every "
            "tenant-scoped operation must run inside use_principal(...). If "
            "this is background work, run it as an explicit principal — "
            "SYSTEM for platform maintenance, or the owning user for work "
            "done on their behalf."
        )
    return LOCAL


def identity_enabled() -> bool:
    """Whether multi-user identity is switched on (`identity.enabled`).

    False keeps a personal install working exactly as before: everything is
    LOCAL. True makes a missing principal an error rather than a guess.
    """
    try:
        from gyrfalcon.config import cfg_get

        return bool(cfg_get("identity.enabled", False))
    except Exception:
        return False


@contextlib.contextmanager
def use_principal(principal: Optional[Principal]) -> Iterator[Optional[Principal]]:
    """Bind an identity for the duration of a block.

    Set at the edge — the request handler, the gateway adapter, the CLI entry
    point — and inherited by everything the block calls, including threads
    started through machinery that copies the context.
    """
    token = _PRINCIPAL.set(principal)
    try:
        yield principal
    finally:
        _PRINCIPAL.reset(token)


def as_system() -> "contextlib.AbstractContextManager[Optional[Principal]]":
    """Run platform-owned work under the SYSTEM identity.

    Explicit on purpose: background machinery has to *say* it is acting for
    the platform, so that "no principal" never quietly becomes "all
    privileges".
    """
    return use_principal(SYSTEM)
