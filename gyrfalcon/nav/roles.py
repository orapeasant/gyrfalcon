"""Which role is active, and what that does and does not change.

Spec: `17-users-roles-menus.md` §4.

A membership grants a *set* of role names; exactly one is active at a time,
and switching swaps the sidebar and the function grants. It does not change
row scope — that stays on the membership, because a user who switches to a
narrower role and finds their own flow runs missing will report it as data
loss, and they will be right (§4.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gyrfalcon.identity import Principal
from gyrfalcon.nav.models import Role, is_live


class RoleNotGrantedError(PermissionError):
    """A switch to a role the membership does not carry.

    A `PermissionError` rather than a validation error on purpose: the client
    asking for it is claiming access it was not given, and the answer is 403
    rather than 400 — the request is well-formed, it is simply not allowed.
    """


@dataclass(frozen=True)
class RoleOption:
    """One entry in the role switcher."""

    name: str
    label: str
    grants_nav: bool

    def to_json(self) -> dict:
        return {"name": self.name, "label": self.label,
                "grants_nav": self.grants_nav}


def selectable_roles(store, principal: Principal) -> list[RoleOption]:
    """The live, nav-granting roles this principal may switch between.

    The intersection of what the membership grants (`principal.roles`) and what
    the tenant actually defines. A role string with no `auth_roles` row — such
    as `"operator"` on an install that has never configured navigation — is not
    offered, because selecting it would produce an empty sidebar with no
    explanation.
    """
    granted = set(principal.roles)
    out: list[RoleOption] = []
    for role in store.list_roles():
        if role.name not in granted or not is_live(role) or not role.grants_nav:
            continue
        out.append(RoleOption(name=role.name, label=role.display_label,
                              grants_nav=True))
    return out


def resolve_active_role(store, principal: Principal,
                        remembered: Optional[str] = None) -> Optional[str]:
    """Which role this session should start on (§4.1).

    1. `remembered` (the membership's `active_role`), if it is still granted
       and still live. A role revoked or expired since the last login must not
       keep resolving just because it was written down.
    2. Otherwise the first selectable role by label — deterministic, so two
       logins never disagree about where a person lands.
    3. Otherwise None, which callers render as an explicit "no access is
       assigned to your account". Never a blank sidebar that reads as a broken
       page, and never a fallback to showing everything.
    """
    options = selectable_roles(store, principal)
    if not options:
        return None
    if remembered is not None:
        if any(o.name == remembered for o in options):
            return remembered
    return sorted(options, key=lambda o: (o.label or o.name))[0].name


def assert_may_switch(store, principal: Principal, role_name: str) -> Role:
    """Validate a requested switch, or raise.

    The client's claim is never trusted: this checks the membership and the
    role's own liveness server-side. It is the one place the *granted set* is
    enforced, which §4.3 identifies as the real security boundary — the active
    role is least-privilege ergonomics within it, not a barrier of its own.
    """
    if role_name not in set(principal.roles):
        raise RoleNotGrantedError(
            f"role {role_name!r} is not granted to this account"
        )
    role = store.get_role_by_name(role_name)
    if role is None or not is_live(role):
        raise RoleNotGrantedError(
            f"role {role_name!r} is not currently active"
        )
    return role
