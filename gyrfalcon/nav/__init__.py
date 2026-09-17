"""Navigation and access control.

Spec: `docs/spec/gyrfalcon/17-users-roles-menus.md`.

A Role owns one Menu; a Menu resolves recursively through its items to
Functions; that resolved set is both the sidebar tree and the external invoke
permission set. One derivation, two consumers.

    from gyrfalcon.nav import get_nav_store, resolve_active_role

    store = get_nav_store()
    role = resolve_active_role(store, principal, remembered)
    tree, grants = store.resolve_role(role)

Module map:

* `models`   — rows, resolved nodes, access levels, `is_live`
* `resolver` — the recursive walk, cycle detection, grant flattening
* `store`    — persistence, and the invariants a write must satisfy
* `grants`   — the version-invalidated permission cache
* `roles`    — active-role resolution and switch validation
* `seed`     — the default pages, menus and roles for a fresh tenant
"""

from gyrfalcon.nav.grants import GrantCache, ResolvedGrants, get_grant_cache  # noqa: F401
from gyrfalcon.nav.models import (  # noqa: F401
    ACCESS_LEVELS,
    FUNCTION_KINDS,
    INVOCABLE_KINDS,
    READ,
    WRITE,
    Function,
    Grant,
    Group,
    Leaf,
    Menu,
    MenuItem,
    Page,
    Role,
    is_live,
    satisfies,
)
from gyrfalcon.nav.resolver import (  # noqa: F401
    MAX_DEPTH,
    CycleError,
    flatten_grants,
    resolve,
    resolve_tree,
    would_cycle,
)
from gyrfalcon.nav.roles import (  # noqa: F401
    RoleNotGrantedError,
    RoleOption,
    assert_may_switch,
    resolve_active_role,
    selectable_roles,
)
from gyrfalcon.nav.seed import ensure_pages, ensure_seeded  # noqa: F401
from gyrfalcon.nav.store import (  # noqa: F401
    NavStore,
    NavStructureError,
    get_nav_store,
    set_nav_store,
)

__all__ = [
    "NavStore", "NavStructureError", "get_nav_store", "set_nav_store",
    "resolve", "resolve_tree", "flatten_grants", "would_cycle", "CycleError",
    "MAX_DEPTH", "GrantCache", "ResolvedGrants", "get_grant_cache",
    "resolve_active_role", "selectable_roles", "assert_may_switch",
    "RoleOption", "RoleNotGrantedError",
    "ensure_seeded", "ensure_pages",
    "Page", "Function", "Menu", "MenuItem", "Role", "Leaf", "Group", "Grant",
    "is_live", "satisfies", "READ", "WRITE", "ACCESS_LEVELS",
    "FUNCTION_KINDS", "INVOCABLE_KINDS",
]
