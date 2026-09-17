"""The vocabulary: rows, resolved nodes, and liveness.

Spec: `17-users-roles-menus.md` §2, §3, §6.

Kept separate from the store so the resolver can be tested against plain
objects, and so "what is a Function" has one answer rather than one per
module that happens to read the table.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# ── Access ───────────────────────────────────────────────────────────────────

READ = "read"
WRITE = "write"
ACCESS_LEVELS = (READ, WRITE)


def satisfies(granted: str, required: str) -> bool:
    """Whether `granted` is enough for `required`.

    Two levels, not a CRUD matrix (§5.4): `write` satisfies a `read`
    requirement, `read` never satisfies `write`. "May enumerate and watch, may
    not trigger" is the distinction integrations actually ask for.
    """
    if required == READ:
        return granted in (READ, WRITE)
    return granted == WRITE


# ── Function kinds ───────────────────────────────────────────────────────────

KIND_PAGE = "page"
KIND_FLOW = "flow"
KIND_AGENT = "agent"
KIND_SKILL = "skill"
KIND_MCP = "mcp"
KIND_CODE = "code"

FUNCTION_KINDS = (KIND_PAGE, KIND_FLOW, KIND_AGENT, KIND_SKILL, KIND_MCP, KIND_CODE)

#: Kinds that can be invoked over the external `/v1` surface. `page` is absent
#: on purpose — there is no external verb for "look at a React route", so a
#: page Function grants navigation and nothing else (§5.1).
#:
#: `code` is absent for a different reason: its target format and whether it
#: executes server-side are still unresolved (§13.3). Admitting it here would
#: quietly make a menu entry a remote-code-execution surface, so it stays out
#: until that decision is made.
INVOCABLE_KINDS = (KIND_FLOW, KIND_AGENT, KIND_SKILL, KIND_MCP)


# ── Liveness ─────────────────────────────────────────────────────────────────

def _field(obj: Any, name: str) -> Any:
    """Read `name` off either a database row or one of the dataclasses below.

    Both shapes reach `is_live`: the resolver works in dataclasses, while
    seeding and migration code sometimes has a raw row in hand. Supporting both
    here is cheaper than converting at every call site, and stops a missing
    `active_from` on `Page` — which genuinely has no window — from being a
    special case.
    """
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def is_live(row: Any, now: Optional[float] = None) -> bool:
    """`enabled`, and inside `active_from`/`active_to` if either is set (§6).

    Never cached into a column: "active" changes with the clock, not with a
    write, so a row that goes live at midnight must do so without anyone
    touching it. Rows that carry no window (pages) answer on `enabled` alone.
    """
    if row is None:
        return False
    if not _field(row, "enabled"):
        return False
    moment = time.time() if now is None else now
    start = _field(row, "active_from")
    end = _field(row, "active_to")
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True


# ── Rows ─────────────────────────────────────────────────────────────────────

def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Page:
    """A navigable route that exists in this build. Global, not per-tenant."""

    id: str
    key: str
    route: str
    label: str
    icon: Optional[str]
    enabled: bool
    created_at: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Page":
        return cls(id=row["id"], key=row["key"], route=row["route"],
                   label=row["label"], icon=row["icon"],
                   enabled=bool(row["enabled"]), created_at=row["created_at"])


@dataclass(frozen=True)
class Function:
    """A thing a menu entry points at, and the unit of external authorization."""

    id: str
    key: str
    name: str
    icon: Optional[str]
    kind: str
    target: str
    params: Optional[dict]
    enabled: bool
    active_from: Optional[float]
    active_to: Optional[float]
    tenant_id: str

    @property
    def is_invocable(self) -> bool:
        return self.kind in INVOCABLE_KINDS

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Function":
        return cls(id=row["id"], key=row["key"], name=row["name"],
                   icon=row["icon"], kind=row["kind"], target=row["target"],
                   params=_loads(row["params"]), enabled=bool(row["enabled"]),
                   active_from=row["active_from"], active_to=row["active_to"],
                   tenant_id=row["tenant_id"])


@dataclass(frozen=True)
class Menu:
    id: str
    name: str
    icon: Optional[str]
    enabled: bool
    active_from: Optional[float]
    active_to: Optional[float]
    tenant_id: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Menu":
        return cls(id=row["id"], name=row["name"], icon=row["icon"],
                   enabled=bool(row["enabled"]), active_from=row["active_from"],
                   active_to=row["active_to"], tenant_id=row["tenant_id"])


@dataclass(frozen=True)
class MenuItem:
    """A leaf (`function_id`) or a branch (`ref_menu_id`). Exactly one is set."""

    id: str
    menu_id: str
    sort_order: int
    function_id: Optional[str]
    ref_menu_id: Optional[str]
    access: str
    label_override: Optional[str]
    icon_override: Optional[str]
    enabled: bool
    active_from: Optional[float]
    active_to: Optional[float]
    tenant_id: str

    @property
    def is_branch(self) -> bool:
        return self.ref_menu_id is not None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MenuItem":
        return cls(id=row["id"], menu_id=row["menu_id"],
                   sort_order=row["sort_order"], function_id=row["function_id"],
                   ref_menu_id=row["ref_menu_id"], access=row["access"] or WRITE,
                   label_override=row["label_override"],
                   icon_override=row["icon_override"],
                   enabled=bool(row["enabled"]), active_from=row["active_from"],
                   active_to=row["active_to"], tenant_id=row["tenant_id"])


@dataclass(frozen=True)
class Role:
    """A named bundle of access carrying exactly one Menu."""

    id: str
    name: str
    label: Optional[str]
    menu_id: Optional[str]
    enabled: bool
    active_from: Optional[float]
    active_to: Optional[float]
    description: Optional[str]
    tenant_id: str

    @property
    def display_label(self) -> str:
        return self.label or self.name

    @property
    def grants_nav(self) -> bool:
        return self.menu_id is not None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Role":
        return cls(id=row["id"], name=row["name"], label=row["label"],
                   menu_id=row["menu_id"], enabled=bool(row["enabled"]),
                   active_from=row["active_from"], active_to=row["active_to"],
                   description=row["description"], tenant_id=row["tenant_id"])


# ── Resolved nodes ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Leaf:
    """A resolved Function placement: what to render, and what it authorizes."""

    function_key: str
    kind: str
    target: str
    params: Optional[dict]
    label: str
    icon: Optional[str]
    access: str
    #: Only set for `kind="page"` — the route to navigate to, resolved from
    #: `nav_pages` so the tree never carries a route that does not exist.
    route: Optional[str] = None

    @property
    def is_invocable(self) -> bool:
        return self.kind in INVOCABLE_KINDS

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "type": "item", "function_key": self.function_key,
            "kind": self.kind, "target": self.target, "label": self.label,
            "icon": self.icon, "access": self.access,
        }
        if self.route is not None:
            out["path"] = self.route
        if self.params:
            out["params"] = self.params
        return out


@dataclass(frozen=True)
class Group:
    """A resolved sub-menu. Renders as `NavGroup` in the sidebar."""

    menu_id: str
    label: str
    icon: Optional[str]
    items: Sequence["Node"] = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {"type": "group", "label": self.label, "icon": self.icon,
                "items": [i.to_json() for i in self.items]}


Node = Any  # Leaf | Group — a union alias kept loose to avoid a forward-ref dance


@dataclass(frozen=True)
class Grant:
    """One entry in the flattened permission set.

    Carries `kind` and `target` rather than only an access level, because the
    resource-shaped `/v1` routes are authorized by matching `(kind, target)`
    while `/v1/functions/{key}` is authorized by the key — and both read the
    same grant (§5.2).
    """

    function_key: str
    kind: str
    target: str
    access: str
    params: Optional[dict] = None

    @property
    def is_invocable(self) -> bool:
        return self.kind in INVOCABLE_KINDS
