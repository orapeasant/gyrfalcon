"""The permission set, cached and invalidated by version.

Spec: `17-users-roles-menus.md` §5.5.

An authorization check sits on the request path, so it cannot re-walk a menu
tree per call. It also cannot sit behind a TTL: an admin who revokes a role
expects that to be true immediately, and "effective within five minutes" is
not a thing anyone wants to explain during an incident.

So the cache is keyed `(tenant_id, role_name)` and validated against
`nav_versions.version`, which every nav write bumps in its own transaction
(`NavStore._bump`). The cost per request is one integer read; the benefit is
that a multi-process SERVER converges on the next request rather than on a
timer, with no invalidation message to deliver between processes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from gyrfalcon.nav.models import Grant, satisfies


@dataclass(frozen=True)
class ResolvedGrants:
    """One role's resolved permission set, and the version it was built at."""

    tenant_id: str
    role_name: str
    version: int
    grants: dict[str, Grant]

    def allows_function(self, function_key: str, required: str) -> bool:
        """Whether this set permits `function_key` at `required` access."""
        grant = self.grants.get(function_key)
        if grant is None or not grant.is_invocable:
            return False
        return satisfies(grant.access, required)

    def allows_target(self, kind: str, target: str, required: str) -> bool:
        """Whether this set permits a resource-shaped call (§5.2).

        `POST /v1/flows/nightly_ingest/runs` is allowed iff some granted
        Function has `kind="flow"`, `target="nightly_ingest"` and sufficient
        access. Several Functions may share a target — "Run Ingest (EU)" and
        "(US)" differ only in `params` — so the highest access among them wins.
        """
        best: Optional[str] = None
        for grant in self.grants.values():
            if grant.kind != kind or grant.target != target:
                continue
            if not grant.is_invocable:
                continue
            if satisfies(grant.access, required):
                return True
            best = grant.access
        return best is not None and satisfies(best, required)

    def invocable(self) -> list[Grant]:
        """What `GET /v1/functions` returns: the invocable half of the set.

        Page grants are excluded — there is no external verb for a route
        (§5.1) — which is why an integration's listing and its permissions are
        the same thing and there is no "visible but 403" state to explain.
        """
        return [g for g in self.grants.values() if g.is_invocable]


class GrantCache:
    """Process-wide, version-validated. Safe to share across request threads."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ResolvedGrants] = {}
        self._lock = threading.Lock()

    def get(self, store, tenant_id: str, role_name: str) -> ResolvedGrants:
        """Resolved grants for a role, rebuilding only when the tenant's nav
        has changed since they were last built."""
        version = store.version(tenant_id)
        key = (tenant_id, role_name)

        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and hit.version == version:
                return hit

        # Resolved outside the lock: a deep tree is several queries, and
        # holding a process-wide lock across them would serialize every
        # request that shares a tenant. A concurrent duplicate resolve is
        # cheap and produces an identical answer.
        _tree, grants = store.resolve_role(role_name)
        resolved = ResolvedGrants(tenant_id=tenant_id, role_name=role_name,
                                  version=version, grants=grants)

        with self._lock:
            current = self._entries.get(key)
            if current is None or current.version <= version:
                self._entries[key] = resolved
        return resolved

    def invalidate(self, tenant_id: Optional[str] = None) -> None:
        """Drop cached sets. Only needed for tests and for a store swap —
        ordinary writes invalidate by bumping the version instead."""
        with self._lock:
            if tenant_id is None:
                self._entries.clear()
                return
            for key in [k for k in self._entries if k[0] == tenant_id]:
                del self._entries[key]


_CACHE = GrantCache()


def get_grant_cache() -> GrantCache:
    return _CACHE
