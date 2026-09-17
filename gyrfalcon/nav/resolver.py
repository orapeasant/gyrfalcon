"""One walk, two products: the sidebar tree and the permission set.

Spec: `17-users-roles-menus.md` §3.

The whole design rests on this file. A Role owns one Menu; a Menu resolves
recursively through its items to Functions; that resolved set is *both* what
the sidebar renders and what an external invoke is checked against. Deriving
them separately would be two sources of truth for one question, and they would
drift the first time someone edited a menu and forgot the other one.

The resolver is deliberately free of caching and of database specifics: it
takes a small read-only protocol (`Source`) and returns plain objects, so it
can be tested against dicts and so the cache in `grants.py` has something
pure to cache.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol, Sequence

from gyrfalcon.nav.models import (
    KIND_PAGE,
    READ,
    WRITE,
    Function,
    Grant,
    Group,
    Leaf,
    Menu,
    MenuItem,
    Node,
    is_live,
)

#: How deep a menu may nest before the resolver treats it as a data bug. Eight
#: is far beyond any sidebar a person would build; it exists to stop a cycle
#: that slipped past the write-time check from hanging a request.
MAX_DEPTH = 8


class CycleError(RuntimeError):
    """A menu reaches itself. A data bug, never a degraded state to paper over.

    Surfacing this as a 500 rather than silently truncating the tree is
    deliberate: a truncated sidebar looks like a permissions problem and would
    be debugged as one, while the actual fault is a row that should never have
    been written.
    """


class Source(Protocol):
    """The reads a resolution needs. `NavStore` satisfies this."""

    def get_menu(self, menu_id: str) -> Optional[Menu]: ...
    def list_menu_items(self, menu_id: str) -> Sequence[MenuItem]: ...
    def get_function(self, function_id: str) -> Optional[Function]: ...
    def get_page_route(self, page_key: str) -> Optional[str]:
        """The route for a live page, or None if it is missing or disabled."""


def resolve_tree(source: Source, menu_id: str, *, now: Optional[float] = None,
                 _depth: int = 0, _seen: tuple[str, ...] = ()) -> list[Node]:
    """The nested, ordered tree a sidebar renders.

    Liveness is applied at every layer (§6): a disabled Function hides the item
    that points at it, a disabled Menu contributes nothing, and a page Function
    whose route is gone is dropped rather than rendered into a 404.

    An empty branch renders nothing at all. A group header with no reachable
    children reads as a bug to the person looking at it, and it would be one:
    the honest rendering of "you were granted nothing under here" is absence.
    """
    if _depth > MAX_DEPTH or menu_id in _seen:
        raise CycleError(
            f"menu {menu_id} reaches itself (depth {_depth}, path {list(_seen)})"
        )

    menu = source.get_menu(menu_id)
    if not is_live(menu, now):
        return []

    out: list[Node] = []
    for item in source.list_menu_items(menu_id):
        if not is_live(item, now):
            continue

        if item.is_branch:
            ref_menu_id = item.ref_menu_id
            assert ref_menu_id is not None          # is_branch established this
            children = resolve_tree(
                source, ref_menu_id, now=now,
                _depth=_depth + 1, _seen=(*_seen, menu_id),
            )
            if not children:
                continue
            child_menu = source.get_menu(ref_menu_id)
            out.append(Group(
                menu_id=ref_menu_id,
                label=item.label_override or (child_menu.name if child_menu else ""),
                icon=item.icon_override or (child_menu.icon if child_menu else None),
                items=tuple(children),
            ))
            continue

        if item.function_id is None:
            # Neither a leaf nor a branch. The store refuses to write this, so
            # reaching it means the row was edited outside the application.
            continue

        function = source.get_function(item.function_id)
        if function is None or not is_live(function, now):
            continue

        route = None
        if function.kind == KIND_PAGE:
            route = source.get_page_route(function.target)
            if route is None:
                continue        # withdrawn or disabled route: never render a 404

        out.append(Leaf(
            function_key=function.key,
            kind=function.kind,
            target=function.target,
            params=function.params,
            label=item.label_override or function.name,
            icon=item.icon_override or function.icon,
            access=item.access,
            route=route,
        ))

    return out


def flatten_grants(nodes: Iterable[Node]) -> dict[str, Grant]:
    """The same walk, flattened to `{function_key: Grant}`.

    Tree shape is irrelevant to authorization — only reachability is. A
    Function reachable twice at different depths (say, granted `read` in one
    sub-menu and `write` in another) resolves to the **highest** access, on the
    principle that a grant is a grant: having been given write access
    somewhere, it would be surprising to have it withdrawn by the existence of
    a second, narrower placement.

    Page-kind Functions are included. They carry no external invoke authority
    (`Grant.is_invocable` is False for them, and §5.1 is explicit that there is
    no external verb for a route), but the dashboard reads this map to decide
    whether to offer a Delete button, and that question is just as real for a
    page as for a flow.
    """
    grants: dict[str, Grant] = {}
    _collect(nodes, grants)
    return grants


def _collect(nodes: Iterable[Node], into: dict[str, Grant]) -> None:
    for node in nodes:
        if isinstance(node, Group):
            _collect(node.items, into)
            continue
        existing = into.get(node.function_key)
        if existing is not None and existing.access == WRITE:
            continue                      # already the highest; nothing to raise
        if existing is not None and node.access == READ:
            continue
        into[node.function_key] = Grant(
            function_key=node.function_key,
            kind=node.kind,
            target=node.target,
            access=node.access,
            params=node.params,
        )


def resolve(source: Source, menu_id: str, *, now: Optional[float] = None
            ) -> tuple[list[Node], dict[str, Grant]]:
    """Both products from one walk. The normal entry point."""
    tree = resolve_tree(source, menu_id, now=now)
    return tree, flatten_grants(tree)


# ── Cycle prevention, at write time ──────────────────────────────────────────

def reachable_menus(source: Source, menu_id: str, *, _seen: Optional[set] = None
                    ) -> set[str]:
    """Every menu reachable from `menu_id`, **ignoring liveness**.

    Liveness is deliberately not applied. A disabled branch still closes a
    cycle the moment someone re-enables it, so a write-time check that skipped
    disabled rows would happily accept a graph that becomes infinite later —
    and the person who eventually re-enables that branch would have no reason
    to connect the outage to an edit made weeks earlier.
    """
    seen = set() if _seen is None else _seen
    if menu_id in seen:
        return seen
    seen.add(menu_id)
    for item in source.list_menu_items(menu_id):
        if item.is_branch and item.ref_menu_id is not None \
                and item.ref_menu_id not in seen:
            reachable_menus(source, item.ref_menu_id, _seen=seen)
    return seen


def would_cycle(source: Source, owning_menu_id: str, ref_menu_id: str) -> bool:
    """Whether adding `owning_menu -> ref_menu` would close a loop.

    True when the reference points at the owning menu itself, or at any menu
    from which the owning menu is reachable. This is the check the admin UI
    uses to *filter* the "add sub-menu" picker, so the constraint shows up as a
    shorter list rather than as an error after saving.
    """
    if owning_menu_id == ref_menu_id:
        return True
    return owning_menu_id in reachable_menus(source, ref_menu_id)
