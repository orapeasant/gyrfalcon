"""Row visibility — the predicate every tenant-scoped query must carry.

Spec: 15-flow.md §17.5.

The realistic way multi-tenancy leaks is not a missing auth check on a new
endpoint. It is one `SELECT` somewhere that forgot `AND tenant_id = ?`.
Twenty more queries from now, "remember the filter" is not a strategy.

So the filter is not written per query. It is composed here, in one place, and
`sql.compose_where()` is the only thing in the package that assembles a WHERE
clause — which makes "does this query filter by tenant" a question about one
function rather than about every call site. `tests/flow/test_scope.py` asserts
that mechanically.

Reading across tenants is legitimate in exactly three places (crash
reconciliation, the runner's due-deployment scan, schema migration), and those
say so out loud with `Scope.system(reason=...)`. That makes an unscoped read
greppable and reviewable instead of indistinguishable from a forgotten filter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

from gyrfalcon.identity import Principal, require_principal


@dataclass(frozen=True)
class Scope:
    """Which rows the caller may see.

    Three shapes, in increasing breadth:

    * **owner** — `tenant_id = ? AND user_id = ?`. The default for a person.
    * **tenant** — `tenant_id = ?`. An operator or admin, who is responsible
      for the whole tenant and needs to see work they did not start (§17.6).
    * **system** — no predicate at all. Platform machinery only, and it must
      name a reason.
    """

    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    reason: str = ""

    @classmethod
    def of(cls, principal: Optional[Principal] = None) -> "Scope":
        """The scope a principal is entitled to. Defaults to the current one."""
        who = principal or require_principal()
        if who.is_operator:
            return cls(tenant_id=who.tenant_id)
        return cls(tenant_id=who.tenant_id, user_id=who.user_id)

    @classmethod
    def system(cls, reason: str) -> "Scope":
        """Every row in every tenant.

        `reason` is mandatory and is not decoration: it is what makes an
        unscoped query show up as a deliberate decision in review and in
        `grep`, rather than as a filter someone forgot.
        """
        if not reason:
            raise ValueError("Scope.system() requires a reason")
        return cls(reason=reason)

    @property
    def is_system(self) -> bool:
        return self.tenant_id is None

    def tenant_wide(self) -> "Scope":
        """Drop the owner narrowing, keep the tenant (§17.5, 17-users-roles-menus §2).

        For tables that hold tenant *configuration* rather than user-owned
        rows — `nav_menus`, `nav_functions`, `nav_menu_items`, `auth_roles`.
        Those have a `tenant_id` and deliberately no `user_id`: a menu is not
        owned by whoever happened to create it, it belongs to the org, and
        every member resolves the same one. Without this, `Scope.of()` for a
        non-operator emits `AND user_id = ?` against a column that does not
        exist, and the read fails.

        The alternative — adding a `user_id` column to those tables to satisfy
        this helper — was rejected: it would model the data around the shape of
        a filter, and invite the bug where a user can only see menus they
        authored.

        This widens owner → tenant. It never widens across tenants; the
        tenant predicate is untouched. Call sites do not choose it — the nav
        SQL builders apply it, so "which tables are tenant-wide" is a property
        of the table rather than something each query remembers.
        """
        return replace(self, user_id=None)

    def predicate(self, table: str = "") -> tuple[list[str], list[Any]]:
        """SQL fragments and bound values for this scope.

        `table` qualifies the columns when a statement names more than one.
        """
        if self.is_system:
            return [], []
        prefix = f"{table}." if table else ""
        clauses = [f"{prefix}tenant_id = ?"]
        params: list[Any] = [self.tenant_id]
        if self.user_id is not None:
            clauses.append(f"{prefix}user_id = ?")
            params.append(self.user_id)
        return clauses, params

    def owns(self, row: Any) -> bool:
        """Whether a row already in hand is inside this scope.

        For checks that happen after a read rather than inside one — the
        authorization half of §17.6, where "may I see it" and "may I act on
        it" are different questions about the same row.
        """
        if self.is_system:
            return True
        if row is None:
            return False
        if row["tenant_id"] != self.tenant_id:
            return False
        return self.user_id is None or row["user_id"] == self.user_id


def current_scope(scope: Optional[Scope] = None) -> Scope:
    """Resolve an optional scope argument to a concrete one."""
    return scope if scope is not None else Scope.of()


def merge(scope: Scope, clauses: Sequence[str] = (),
          table: str = "") -> tuple[str, tuple]:
    """Compose a full WHERE clause from a scope plus a query's own filters.

    The single place a WHERE is built. Returns the clause and *only the
    scope's* bound values: scope predicates are always emitted first, so a
    caller supplies its own values by appending them —
    `conn.fetchone(sql, (*scope_params, run_id))`. Threading caller values
    through here as `None` placeholders was the obvious alternative and is
    worse: every call site then has to remember which slot to overwrite.
    """
    scope_clauses, scope_params = scope.predicate(table)
    all_clauses = [*scope_clauses, *clauses]
    where = ("WHERE " + " AND ".join(all_clauses)) if all_clauses else ""
    return where, tuple(scope_params)
