"""Persistence for pages, functions, menus, items and roles.

Spec: `17-users-roles-menus.md` §2.

Shares the flow database, dialect layer and migration counter, like
`auth/store.py` — one DSN, one schema version, no way to end up with half of
this subsystem present.

Two invariants live here rather than in the callers, because a caller that
forgets either produces data the resolver cannot make sense of:

1. **A menu item is a leaf or a branch, never both and never neither.** The
   schema cannot say this without a CHECK constraint, and a CHECK would have to
   be spelled differently per backend (§15.3a).
2. **Every write bumps `nav_versions` in the same transaction.** That is what
   makes a revoked role take effect on the next request in every process,
   rather than after a cache TTL expires somewhere.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Optional, Sequence

from gyrfalcon.db import open_database, resolve_target, sql
from gyrfalcon.db.migrations import ensure_schema
from gyrfalcon.db.scope import Scope, current_scope
from gyrfalcon.nav import resolver
from gyrfalcon.nav.models import (
    WRITE,
    Function,
    Menu,
    MenuItem,
    Page,
    Role,
    is_live,
)


def _new_id() -> str:
    return uuid.uuid4().hex


def _tenant_of(scope: Scope) -> str:
    """The tenant a write belongs to, or an error.

    `Scope.system()` has no tenant by design — it is the cross-tenant *read*
    escape hatch. Writing navigation configuration under it would have no
    coherent meaning (whose menu is it?), so this refuses rather than silently
    filing the row under the string "None".
    """
    if scope.tenant_id is None:
        raise NavStructureError(
            "navigation configuration belongs to a tenant; a system scope "
            "cannot create or edit it"
        )
    return scope.tenant_id


class NavStructureError(ValueError):
    """A write that would produce a tree the resolver cannot walk."""


class NavStore:
    """Tenant-scoped navigation configuration.

    Every read of the four tenant tables goes through a `sql` builder taking a
    `Scope`; `nav_pages` is global and does not. Instances are cheap and
    stateless — the process-wide one from `get_nav_store()` is the norm.
    """

    def __init__(self, db_path=None, backend: Optional[str] = None,
                 dsn: Optional[str] = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self._db = open_database(backend=self.backend, path=resolved, dsn=self.dsn)
        self.schema_version = ensure_schema(self._db)

    # -- version counter ----------------------------------------------------

    def version(self, tenant_id: str) -> int:
        """The tenant's nav version. 0 when nothing has been written yet."""
        with self._db.connect() as conn:
            row = conn.fetchone(sql.GET_NAV_VERSION, (tenant_id,))
        return int(row["version"]) if row else 0

    def _bump(self, conn, tenant_id: str) -> None:
        """Invalidate every cached grant set for this tenant.

        Called inside the caller's transaction, never after it: a bump that
        committed separately could be lost while the write survived, leaving
        processes serving permissions that no longer exist.
        """
        now = time.time()
        conn.execute(sql.insert_nav_version(self._db.dialect), (tenant_id, 0, now))
        conn.execute(sql.BUMP_NAV_VERSION, (now, tenant_id))

    # -- pages (global) -----------------------------------------------------

    def create_page(self, key: str, route: str, label: str,
                    icon: Optional[str] = None, enabled: bool = True) -> Page:
        page_id = _new_id()
        with self._db.connect() as conn:
            conn.execute(
                sql.insert_page(self._db.dialect),
                (page_id, key, route, label, icon, 1 if enabled else 0, time.time()),
            )
        found = self.get_page_by_key(key)
        if found is None:                       # pragma: no cover - insert_ignore raced
            raise NavStructureError(f"page {key!r} could not be created")
        return found

    def get_page(self, page_id: str) -> Optional[Page]:
        with self._db.connect() as conn:
            row = conn.fetchone(sql.GET_PAGE, (page_id,))
        return Page.from_row(row) if row else None

    def get_page_by_key(self, key: str) -> Optional[Page]:
        with self._db.connect() as conn:
            row = conn.fetchone(sql.GET_PAGE_BY_KEY, (key,))
        return Page.from_row(row) if row else None

    def list_pages(self) -> list[Page]:
        with self._db.connect() as conn:
            return [Page.from_row(r) for r in conn.fetchall(sql.LIST_PAGES)]

    def update_page(self, page_id: str, route: str, label: str,
                    icon: Optional[str], enabled: bool) -> Optional[Page]:
        with self._db.connect() as conn:
            conn.execute(sql.UPDATE_PAGE,
                         (route, label, icon, 1 if enabled else 0, page_id))
        return self.get_page(page_id)

    def delete_page(self, page_id: str) -> bool:
        with self._db.connect() as conn:
            cur = conn.execute(sql.DELETE_PAGE, (page_id,))
            return bool(getattr(cur, "rowcount", 0))

    # -- functions ----------------------------------------------------------

    def create_function(self, key: str, name: str, kind: str, target: str, *,
                        icon: Optional[str] = None, params: Optional[dict] = None,
                        enabled: bool = True, active_from: Optional[float] = None,
                        active_to: Optional[float] = None,
                        scope: Optional[Scope] = None) -> Function:
        sc = current_scope(scope).tenant_wide()
        tenant = _tenant_of(sc)
        now = time.time()
        fn_id = _new_id()
        with self._db.connect() as conn:
            conn.execute(
                sql.insert_function(self._db.dialect),
                (fn_id, key, name, icon, kind, target,
                 json.dumps(params) if params else None,
                 1 if enabled else 0, active_from, active_to, now, now, tenant),
            )
            self._bump(conn, tenant)
        found = self.get_function_by_key(key, scope=sc)
        if found is None:                       # pragma: no cover
            raise NavStructureError(f"function {key!r} could not be created")
        return found

    def get_function(self, function_id: str,
                     scope: Optional[Scope] = None) -> Optional[Function]:
        stmt, params = sql.get_function(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*params, function_id))
        return Function.from_row(row) if row else None

    def get_function_by_key(self, key: str,
                            scope: Optional[Scope] = None) -> Optional[Function]:
        stmt, params = sql.get_function_by_key(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*params, key))
        return Function.from_row(row) if row else None

    def list_functions(self, scope: Optional[Scope] = None) -> list[Function]:
        stmt, params = sql.list_functions(current_scope(scope))
        with self._db.connect() as conn:
            return [Function.from_row(r) for r in conn.fetchall(stmt, params)]

    def update_function(self, function_id: str, *, name: str, kind: str,
                        target: str, icon: Optional[str] = None,
                        params: Optional[dict] = None, enabled: bool = True,
                        active_from: Optional[float] = None,
                        active_to: Optional[float] = None,
                        scope: Optional[Scope] = None) -> Optional[Function]:
        sc = current_scope(scope)
        stmt, sp = sql.update_function(sc)
        with self._db.connect() as conn:
            conn.execute(stmt, (name, icon, kind, target,
                                json.dumps(params) if params else None,
                                1 if enabled else 0, active_from, active_to,
                                time.time(), *sp, function_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
        return self.get_function(function_id, scope=sc)

    def delete_function(self, function_id: str,
                        scope: Optional[Scope] = None) -> bool:
        sc = current_scope(scope)
        stmt, sp = sql.delete_function(sc)
        with self._db.connect() as conn:
            cur = conn.execute(stmt, (*sp, function_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
            return bool(getattr(cur, "rowcount", 0))

    # -- menus --------------------------------------------------------------

    def create_menu(self, name: str, *, icon: Optional[str] = None,
                    enabled: bool = True, active_from: Optional[float] = None,
                    active_to: Optional[float] = None,
                    menu_id: Optional[str] = None,
                    scope: Optional[Scope] = None) -> Menu:
        sc = current_scope(scope).tenant_wide()
        now = time.time()
        menu_id = menu_id or _new_id()
        with self._db.connect() as conn:
            conn.execute(
                sql.insert_menu(self._db.dialect),
                (menu_id, name, icon, 1 if enabled else 0, active_from,
                 active_to, now, now, _tenant_of(sc)),
            )
            self._bump(conn, _tenant_of(sc))
        created = self.get_menu(menu_id, scope=sc)
        if created is None:                     # pragma: no cover
            raise NavStructureError(f"menu {menu_id} could not be created")
        return created

    def get_menu(self, menu_id: str, scope: Optional[Scope] = None) -> Optional[Menu]:
        stmt, params = sql.get_menu(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*params, menu_id))
        return Menu.from_row(row) if row else None

    def list_menus(self, scope: Optional[Scope] = None) -> list[Menu]:
        stmt, params = sql.list_menus(current_scope(scope))
        with self._db.connect() as conn:
            return [Menu.from_row(r) for r in conn.fetchall(stmt, params)]

    def update_menu(self, menu_id: str, *, name: str, icon: Optional[str] = None,
                    enabled: bool = True, active_from: Optional[float] = None,
                    active_to: Optional[float] = None,
                    scope: Optional[Scope] = None) -> Optional[Menu]:
        sc = current_scope(scope)
        stmt, sp = sql.update_menu(sc)
        with self._db.connect() as conn:
            conn.execute(stmt, (name, icon, 1 if enabled else 0, active_from,
                                active_to, time.time(), *sp, menu_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
        return self.get_menu(menu_id, scope=sc)

    def delete_menu(self, menu_id: str, scope: Optional[Scope] = None) -> bool:
        """Removes the menu and its own items.

        Items in *other* menus that branch to this one are left in place and
        simply stop resolving (`resolve_tree` drops a branch whose menu is
        gone). Deleting them here would silently edit menus the caller did not
        ask about; `items_referencing_menu` is how an admin UI warns first.
        """
        sc = current_scope(scope)
        del_items, ip = sql.delete_items_of_menu(sc)
        del_menu, mp = sql.delete_menu(sc)
        with self._db.connect() as conn:
            conn.execute(del_items, (*ip, menu_id))
            cur = conn.execute(del_menu, (*mp, menu_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
            return bool(getattr(cur, "rowcount", 0))

    # -- menu items ---------------------------------------------------------

    def add_item(self, menu_id: str, *, function_id: Optional[str] = None,
                 ref_menu_id: Optional[str] = None, access: str = WRITE,
                 sort_order: int = 0, label_override: Optional[str] = None,
                 icon_override: Optional[str] = None, enabled: bool = True,
                 active_from: Optional[float] = None,
                 active_to: Optional[float] = None,
                 scope: Optional[Scope] = None) -> MenuItem:
        """Add a leaf or a branch. Refuses anything that is neither or both,
        and refuses a branch that would close a cycle (§3.1)."""
        if (function_id is None) == (ref_menu_id is None):
            raise NavStructureError(
                "a menu item references exactly one of a function or a menu; "
                f"got function_id={function_id!r}, ref_menu_id={ref_menu_id!r}"
            )
        sc = current_scope(scope).tenant_wide()
        if ref_menu_id is not None:
            view = _StoreSource(self, sc)
            if resolver.would_cycle(view, menu_id, ref_menu_id):
                raise NavStructureError(
                    f"menu {ref_menu_id} already reaches {menu_id}; adding this "
                    "reference would make the tree infinite"
                )
        item_id = _new_id()
        with self._db.connect() as conn:
            conn.execute(
                sql.insert_menu_item(self._db.dialect),
                (item_id, menu_id, sort_order, function_id, ref_menu_id, access,
                 label_override, icon_override, 1 if enabled else 0,
                 active_from, active_to, _tenant_of(sc)),
            )
            self._bump(conn, _tenant_of(sc))
        created = self.get_item(item_id, scope=sc)
        if created is None:                     # pragma: no cover
            raise NavStructureError(f"menu item {item_id} could not be created")
        return created

    def get_item(self, item_id: str, scope: Optional[Scope] = None) -> Optional[MenuItem]:
        stmt, params = sql.get_menu_item(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*params, item_id))
        return MenuItem.from_row(row) if row else None

    def list_items(self, menu_id: str,
                   scope: Optional[Scope] = None) -> list[MenuItem]:
        stmt, params = sql.list_menu_items(current_scope(scope))
        with self._db.connect() as conn:
            return [MenuItem.from_row(r)
                    for r in conn.fetchall(stmt, (*params, menu_id))]

    def items_referencing(self, menu_id: str,
                          scope: Optional[Scope] = None) -> list[MenuItem]:
        stmt, params = sql.items_referencing_menu(current_scope(scope))
        with self._db.connect() as conn:
            return [MenuItem.from_row(r)
                    for r in conn.fetchall(stmt, (*params, menu_id))]

    def update_item(self, item_id: str, *, sort_order: int, access: str,
                    label_override: Optional[str] = None,
                    icon_override: Optional[str] = None, enabled: bool = True,
                    active_from: Optional[float] = None,
                    active_to: Optional[float] = None,
                    scope: Optional[Scope] = None) -> Optional[MenuItem]:
        """Placement only. What an item *points at* is not editable: repointing
        a leaf at a different Function is a different grant wearing the same
        row's id, and it would silently change what every role holding this
        menu may invoke. Delete and re-add instead."""
        sc = current_scope(scope)
        stmt, sp = sql.update_menu_item(sc)
        with self._db.connect() as conn:
            conn.execute(stmt, (sort_order, access, label_override, icon_override,
                                1 if enabled else 0, active_from, active_to,
                                *sp, item_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
        return self.get_item(item_id, scope=sc)

    def reorder(self, ordered_item_ids: Sequence[str],
                scope: Optional[Scope] = None) -> None:
        """Apply an explicit order in one transaction, so the tree is never
        briefly half-reordered for a concurrent reader."""
        sc = current_scope(scope)
        stmt, sp = sql.set_menu_item_order(sc)
        with self._db.connect() as conn:
            for position, item_id in enumerate(ordered_item_ids):
                conn.execute(stmt, (position, *sp, item_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))

    def delete_item(self, item_id: str, scope: Optional[Scope] = None) -> bool:
        sc = current_scope(scope)
        stmt, sp = sql.delete_menu_item(sc)
        with self._db.connect() as conn:
            cur = conn.execute(stmt, (*sp, item_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
            return bool(getattr(cur, "rowcount", 0))

    # -- roles --------------------------------------------------------------

    def create_role(self, name: str, *, label: Optional[str] = None,
                    menu_id: Optional[str] = None, description: Optional[str] = None,
                    enabled: bool = True, active_from: Optional[float] = None,
                    active_to: Optional[float] = None,
                    scope: Optional[Scope] = None) -> Role:
        sc = current_scope(scope).tenant_wide()
        with self._db.connect() as conn:
            conn.execute(
                sql.insert_role(self._db.dialect),
                (_new_id(), name, label or name, menu_id, 1 if enabled else 0,
                 active_from, active_to, description, time.time(), _tenant_of(sc)),
            )
            self._bump(conn, _tenant_of(sc))
        created = self.get_role_by_name(name, scope=sc)
        if created is None:                     # pragma: no cover
            raise NavStructureError(f"role {name!r} could not be created")
        return created

    def get_role(self, role_id: str, scope: Optional[Scope] = None) -> Optional[Role]:
        stmt, params = sql.get_role(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*params, role_id))
        return Role.from_row(row) if row else None

    def get_role_by_name(self, name: str,
                         scope: Optional[Scope] = None) -> Optional[Role]:
        stmt, params = sql.get_role_by_name(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*params, name))
        return Role.from_row(row) if row else None

    def list_roles(self, scope: Optional[Scope] = None) -> list[Role]:
        stmt, params = sql.list_roles(current_scope(scope))
        with self._db.connect() as conn:
            return [Role.from_row(r) for r in conn.fetchall(stmt, params)]

    def update_role(self, role_id: str, *, name: str, label: Optional[str],
                    menu_id: Optional[str], enabled: bool = True,
                    active_from: Optional[float] = None,
                    active_to: Optional[float] = None,
                    description: Optional[str] = None,
                    scope: Optional[Scope] = None) -> Optional[Role]:
        sc = current_scope(scope)
        stmt, sp = sql.update_role(sc)
        with self._db.connect() as conn:
            conn.execute(stmt, (name, label, menu_id, 1 if enabled else 0,
                                active_from, active_to, description,
                                *sp, role_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
        return self.get_role(role_id, scope=sc)

    def delete_role(self, role_id: str, scope: Optional[Scope] = None) -> bool:
        sc = current_scope(scope)
        stmt, sp = sql.delete_role(sc)
        with self._db.connect() as conn:
            cur = conn.execute(stmt, (*sp, role_id))
            self._bump(conn, _tenant_of(sc.tenant_wide()))
            return bool(getattr(cur, "rowcount", 0))

    # -- resolution ---------------------------------------------------------

    def resolve_role(self, role_name: str, *, scope: Optional[Scope] = None,
                     now: Optional[float] = None):
        """`(tree, grants)` for one role, or `([], {})` if it grants nothing.

        A role that is absent, not live, or carries no menu all resolve to the
        same empty answer deliberately: from the caller's side they are one
        situation — "this role confers nothing right now" — and distinguishing
        them here would push three cases into every consumer.
        """
        sc = current_scope(scope).tenant_wide()
        role = self.get_role_by_name(role_name, scope=sc)
        if role is None or not is_live(role, now) or not role.grants_nav:
            return [], {}
        assert role.menu_id is not None      # grants_nav checked it above
        return resolver.resolve(_StoreSource(self, sc), role.menu_id, now=now)

    def close(self) -> None:
        self._db.close()


class _StoreSource:
    """A `resolver.Source` over one store and scope, memoized for one walk.

    The memo matters: `resolve_tree` reads a branch's menu twice (once to
    recurse, once for its label), and a deep tree would otherwise re-query the
    same rows at every level. Scoped to a single resolution, so it can never
    serve a stale row across requests — that is the version counter's job.
    """

    def __init__(self, store: NavStore, scope: Scope):
        self._store = store
        self._scope = scope
        self._menus: dict[str, Optional[Menu]] = {}
        self._items: dict[str, Sequence[MenuItem]] = {}
        self._functions: dict[str, Optional[Function]] = {}
        self._routes: dict[str, Optional[str]] = {}

    def get_menu(self, menu_id: str) -> Optional[Menu]:
        if menu_id not in self._menus:
            self._menus[menu_id] = self._store.get_menu(menu_id, scope=self._scope)
        return self._menus[menu_id]

    def list_menu_items(self, menu_id: str) -> Sequence[MenuItem]:
        if menu_id not in self._items:
            self._items[menu_id] = self._store.list_items(menu_id, scope=self._scope)
        return self._items[menu_id]

    def get_function(self, function_id: str) -> Optional[Function]:
        if function_id not in self._functions:
            self._functions[function_id] = self._store.get_function(
                function_id, scope=self._scope)
        return self._functions[function_id]

    def get_page_route(self, page_key: str) -> Optional[str]:
        if page_key not in self._routes:
            page = self._store.get_page_by_key(page_key)
            self._routes[page_key] = (
                page.route if page is not None and page.enabled else None
            )
        return self._routes[page_key]


_DEFAULT: Optional[NavStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_nav_store() -> NavStore:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = NavStore()
        return _DEFAULT


def set_nav_store(store: Optional[NavStore]) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = store
