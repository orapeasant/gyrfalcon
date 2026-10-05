# FND database objects

`fnd_` is the prefix for Foundation objects and for Gyrfalcon engine-owned
objects. Workflow screens belong to `wf`, but the workflow runtime remains an
engine service and its tables keep the `fnd_flow_*` names. Product-owned data
uses its module prefix (`aie_`, `po_`, `inv_`, `ar_`, etc.).

Application database changes are authored beside the owning module under
`apps/<module>/sql/`. The current Alembic chain is still the deployment entry
point at `gyrfalcon/db/alembic`; `gyrfalcon/db/alembic.ini` includes
`apps/fnd/sql/migrations` in its version search path. Do not run a module migration
by executing its SQL file manually: the migration runner owns ordering,
locking, and the recorded revision state. Engine-owned schema continues to be
defined in `gyrfalcon/db/schema.py` and changed through that same runner.

## Access model

The schema changes in the access-control revision are additive first, so the
current application remains readable during the code cutover:

| Table | Purpose | Key columns |
|---|---|---|
| `fnd_nav_pages` | Built page catalog and DB-owned route | `key`, `app_key`, `route` |
| `fnd_nav_page_aliases` | Explicit old path to registered page | `route`, `page_key` |
| `fnd_nav_functions` | Page/code/flow/agent/skill/MCP capability | `tenant_id`, `key`, `kind`, `target` |
| `fnd_nav_menus` | Reusable nested menu | `tenant_id`, `id` |
| `fnd_nav_menu_items` | Ordered child-menu or Function reference | `tenant_id`, `menu_id`, `ref_menu_id` / `function_id` |
| `fnd_auth_roles` | Role and its root menu | `tenant_id`, `id`, `menu_id` |
| `fnd_auth_user_roles` | Direct role assignment scoped to a tenant | `tenant_id`, `user_id`, `role_id` |
| `fnd_auth_group_roles` | Group role assignment scoped to a tenant | `tenant_id`, `group_id`, `role_id` |
| `fnd_auth_group_provider_mappings` | Provider group ID mapped to an app Group | `tenant_id`, `provider`, `provider_group_id`, `group_id` |

`fnd_auth_memberships.roles` is the legacy source during migration. Backfill
its role names into `fnd_auth_user_roles` by joining role names in the same
tenant. Keep the legacy column until the new resolver and admin UI are deployed
and reconciled; then remove it and `active_role` in a later contract revision.
This avoids an authorization outage during a rolling deployment.

The effective role set is the union of direct user-role rows and roles assigned
to the user's app Groups. Resolve each role's root menu, then deduplicate roots
by menu ID only. Preserve each root's complete authored nested tree. Flatten
the resulting trees for authorization, deduplicating Function IDs and
combining read/write access at the strongest effective level. Do not merge
nested trees across distinct roots.

The current default catalog contains two roles: **Administrator** (stable key
`system_admin`) and **Developer** (stable key `app_developer`). Both point to
the same `Default Menu` root and therefore currently receive identical menu
trees. `operator` remains an internal identity marker for tenant-wide engine
operations; it is not a third menu role. The consolidation revision preserves
that marker while migrating existing seeded users and groups to the shared
Administrator/Developer menu setup.

Group provider mappings are a lookup bridge, not an identity-provider role
grant. Synchronization resolves provider group identifiers through the mapping
table and then updates `fnd_auth_group_memberships`; role grants remain app
configuration in `fnd_auth_group_roles`.

## Frontend page ownership

The initial page keys and route paths are preserved from the current dashboard.
The database stores route paths and `app_key`; the compiled app manifest maps
stable page keys to components. It must never store/import arbitrary component
paths from the database.

| App | Page keys | Current route family |
|---|---|---|
| `aie` | `chat`, `sessions`, `analytics`, `tokenomics.report`, `tokenomics.estimation` | `/chat`, `/sessions`, `/analytics`, `/tokenomics/*` |
| `wf` | `flow.instances`, `flow.tasks`, `flow.definitions`, `flow.designer`, `flow.deployments`, `flow.events` | `/flows/*` |
| `fnd` | `applications.manage`, `ai.models`, `ai.mcp`, `ai.agents`, `ai.skills`, `ai.plugins`, `ai.routing`, `ai.guardrail`, `admin.scheduler`, `admin.profiles`, `admin.config`, `admin.logs`, `security.service_accounts`, `security.secrets`, `security.users`, `security.roles`, `developer.functions`, `developer.menus`, `developer.menu_items` | Existing platform routes and `/developer/*` |

`/`, `/tokenomics`, `/flows`, `/flows/definitions`, `/security`, and
`/applications` are compatibility redirects, not independent page components.
Register them in `fnd_nav_page_aliases` only when their destination page key is
clear and the redirect is intentional.

The frontend source is under `apps/<app>/web`; shared shell, session, API
client, and reusable components live in `packages/web-common`. Build each app
from `web/` with `npm run build:aie`, `npm run build:wf`, or
`npm run build:fnd`; `npm run build:apps` builds all three. Artifacts are
written to `web/dist/{aie,wf,fnd}`. The host resolves a registered route to
`app_key`, serves that app's artifact, and serves its namespaced assets. The
legacy `web/` entry remains a transition build; production app builds use the
three module artifacts.
