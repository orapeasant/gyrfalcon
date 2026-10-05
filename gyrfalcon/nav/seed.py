"""Default pages, functions, menus and roles for a tenant with none.

Spec: `17-users-roles-menus.md` §11.

**Why this is not inside migration 6.** `db/migrations.py` states the rule
it lives by: a migration is a historical fact and must not change when the
schema does. This seed is a transcription of the dashboard's nav, and that nav
grows every time a page ships — putting it in the migration would force a
choice between editing an applied migration (forbidden) and adding one
migration per new page (absurd). So the migration creates structure, and
seeding is idempotent, runs at startup, and no-ops once a tenant has roles.

Idempotence is by *existence*, not by version: `ensure_seeded` returns
immediately if the tenant already has any role. That means an operator who
deliberately deletes the default menus does not get them silently recreated on
the next restart — only a tenant that has never been configured is seeded.
"""

from __future__ import annotations

from gyrfalcon.nav.models import KIND_PAGE, WRITE

# ── The page catalog ─────────────────────────────────────────────────────────
#
# (key, route, label, icon, app_key) — the DB route catalog for each app.
# Application records remain data; their detail view is reached through the
# registered applications.detail route and is not injected into navigation.

PAGES: tuple[tuple[str, str, str, str, str], ...] = (
    ("chat", "/chat", "Chat", "MessageSquare", "aie"),
    ("sessions", "/sessions", "Sessions", "History", "aie"),
    ("analytics", "/analytics", "Analytics", "BarChart2", "aie"),
    ("tokenomics.report", "/tokenomics/report", "Report", "BarChart2", "aie"),
    ("tokenomics.estimation", "/tokenomics/estimate", "Estimation", "Calculator", "aie"),
    ("applications.manage", "/applications/manage", "Applications", "AppWindow", "fnd"),
    ("applications.detail", "/applications/:id", "Application", "AppWindow", "fnd"),
    ("flow.instances", "/flows/instances", "Instances", "Play", "wf"),
    ("flow.tasks", "/flows/tasks", "My Tasks", "Inbox", "wf"),
    ("flow.definitions", "/flows/definitions", "Definitions", "FileCode", "wf"),
    ("flow.designer", "/flows/designer", "Designer", "Workflow", "wf"),
    ("flow.deployments", "/flows/deployments", "Deployments", "CalendarClock", "wf"),
    ("flow.events", "/flows/events", "Events", "Radio", "wf"),
    ("ai.models", "/models", "Models", "Cpu", "fnd"),
    ("ai.mcp", "/mcp", "MCP Servers", "Server", "fnd"),
    ("ai.agents", "/agents", "Agents", "Users", "fnd"),
    ("ai.skills", "/skills", "Skills", "BookOpen", "fnd"),
    ("ai.plugins", "/plugins", "Plugins", "Puzzle", "fnd"),
    ("ai.routing", "/routing", "Routing", "Workflow", "fnd"),
    ("ai.guardrail", "/guardrail", "Guardrail", "ShieldCheck", "fnd"),
    ("admin.scheduler", "/scheduler", "Scheduler", "Clock", "fnd"),
    ("admin.profiles", "/profiles", "Profiles", "Users", "fnd"),
    ("admin.config", "/config", "Configurations", "Settings", "fnd"),
    ("admin.logs", "/logs", "Logs", "ScrollText", "fnd"),
    ("security.service_accounts", "/security/service-accounts",
     "Service Accounts", "KeyRound", "fnd"),
    ("security.secrets", "/security/secrets", "Secret Store", "Lock", "fnd"),
    ("security.users", "/security/users", "Users & Groups", "UsersRound", "fnd"),
    ("security.roles", "/security/roles", "Roles", "ShieldCheck", "fnd"),
    ("developer.functions", "/developer/functions", "Functions", "Workflow", "fnd"),
    ("developer.menus", "/developer/menus", "Menus", "PanelTop", "fnd"),
    ("developer.menu_items", "/developer/menu-items", "Menu Items", "ListTree", "fnd"),
)

# ── Shared sub-menus ─────────────────────────────────────────────────────────
#
# Referenced by both role menus rather than duplicated into each, so adding a
# page to "AI Engine" is one edit that reaches every role holding it (§11.4).
# `items` are page keys; `branches` are other sub-menus by name.

SUBMENUS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (name, icon, page keys, branch menu names)
    ("Flow", "Workflow",
     ("flow.instances", "flow.tasks", "flow.definitions", "flow.designer",
      "flow.deployments", "flow.events"), ()),
    ("Tokenomics", "Calculator",
     ("tokenomics.report", "tokenomics.estimation"), ()),
    ("AI Engine", "Zap",
     ("ai.models", "ai.mcp", "ai.agents", "ai.skills", "ai.plugins",
      "ai.routing", "ai.guardrail"), ()),
    ("Security", "ShieldCheck",
     ("security.service_accounts", "security.secrets", "security.users",
      "security.roles"), ()),
    ("Administration", "Wrench",
     ("admin.scheduler", "admin.profiles", "admin.config", "admin.logs"),
     ("Security",)),
    ("Application Developer", "PanelTop",
     ("applications.manage", "developer.functions", "developer.menus",
      "developer.menu_items"), ()),
)

# ── Role menus, and the roles that carry them ────────────────────────────────

ROLE_MENUS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("Administrator Menu", "Wrench",
     ("chat", "sessions", "analytics", "applications.manage"),
     ("Flow", "Tokenomics", "AI Engine", "Administration")),
    ("Developer Menu", "Wrench",
     ("chat", "sessions", "analytics"),
     ("Flow", "Tokenomics", "AI Engine", "Administration", "Application Developer")),
)

ROLES: tuple[tuple[str, str, str, str], ...] = (
    # (name, label, menu name, description)
    ("system_admin", "Administrator", "Administrator Menu",
     "Administrator access."),
    ("app_developer", "Developer", "Developer Menu",
     "Developer access, including navigation configuration."),
)


def ensure_pages(store) -> int:
    """Register the page catalog. Global, so this runs once per database.

    Returns the number of pages newly created. Safe to call repeatedly: the
    insert ignores a key that already exists, so a page added to this list in a
    later release appears on the next start without disturbing the rest.
    """
    existing = {p.key for p in store.list_pages()}
    created = 0
    for key, route, label, icon, app_key in PAGES:
        if key in existing:
            store.set_page_app_key(key, app_key)
            continue
        store.create_page(key=key, route=route, label=label, icon=icon, app_key=app_key)
        created += 1
    return created


def ensure_seeded(store, scope=None) -> bool:
    """Give a tenant the default menus and roles, if it has none.

    Returns True when seeding ran. False means the tenant was already
    configured and nothing was touched — including the case where an operator
    deliberately removed the defaults.

    Ordering matters and is the reason this is one function rather than four:
    functions need pages, menus need functions, branches need the menus they
    point at, and roles need their menu. Each step depends on the previous
    one's ids.
    """
    ensure_pages(store)

    if store.list_roles(scope=scope):
        return False

    # One `kind="page"` Function per Page. Every nav entry today is a page
    # entry, so the seed introduces no other kind (§11.3); the layer earns its
    # keep the moment someone adds a flow Function by hand.
    functions: dict[str, str] = {}
    for key, _route, label, icon, _app_key in PAGES:
        fn = store.create_function(key=key, name=label, kind=KIND_PAGE,
                                   target=key, icon=icon, scope=scope)
        functions[key] = fn.id

    menus: dict[str, str] = {}
    for name, icon, page_keys, branches in (*SUBMENUS, *ROLE_MENUS):
        menu = store.create_menu(name=name, icon=icon, scope=scope)
        menus[name] = menu.id
        order = 0
        for page_key in page_keys:
            store.add_item(menu.id, function_id=functions[page_key],
                           access=WRITE, sort_order=order,
                           label_override=("Manage Applications"
                                           if page_key == "applications.manage" else None),
                           scope=scope)
            order += 1
        for branch_name in branches:
            store.add_item(menu.id, ref_menu_id=menus[branch_name],
                           sort_order=order, scope=scope)
            order += 1

    for name, label, menu_name, description in ROLES:
        store.create_role(name=name, label=label, menu_id=menus[menu_name],
                          description=description, scope=scope)
    return True
