"""The identity store — orgs, people, membership, and API keys.

Spec: §17.11 step 8.

Shares the flow database, dialect layer and migration counter (see
`flow/db/schema.py`), so a deployment configures one DSN rather than two and
cannot end up with half a schema.

Everything here runs *before* a principal exists — it is what produces one —
so none of it is tenant-scoped and none of it should be reachable from
ordinary request handling. `auth/` is that boundary.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from gyrfalcon.auth import keys as keymod
from gyrfalcon.flow.db import open_database, resolve_target, sql
from gyrfalcon.flow.db.migrations import ensure_schema
from gyrfalcon.identity import Principal


def _new_id() -> str:
    return uuid.uuid4().hex


class AuthStore:
    def __init__(self, db_path=None, backend: Optional[str] = None,
                 dsn: Optional[str] = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self._db = open_database(backend=self.backend, path=resolved, dsn=self.dsn)
        self.schema_version = ensure_schema(self._db)

    # -- organizations -------------------------------------------------------
    def create_org(self, name: str, org_id: Optional[str] = None) -> dict:
        org_id = org_id or _new_id()
        with self._db.connect() as conn:
            conn.execute(sql.INSERT_ORG, (org_id, name, time.time()))
        return self.get_org(org_id)

    def get_org(self, org_id: str) -> Optional[dict]:
        with self._db.connect() as conn:
            return _org(conn.fetchone(sql.GET_ORG, (org_id,)))

    def get_org_by_name(self, name: str) -> Optional[dict]:
        with self._db.connect() as conn:
            return _org(conn.fetchone(sql.GET_ORG_BY_NAME, (name,)))

    def list_orgs(self) -> list[dict]:
        with self._db.connect() as conn:
            return [_org(r) for r in conn.fetchall(sql.LIST_ORGS)]

    # -- people --------------------------------------------------------------
    def upsert_user(self, issuer: str, subject: str, email: str = "",
                    display_name: str = "") -> dict:
        """Find or create the person behind an IdP identity.

        Keyed on `(issuer, subject)` rather than email, because email changes
        and a person's rows must not be orphaned when it does. `subject` is
        the IdP's stable, opaque id for them.
        """
        now = time.time()
        with self._db.connect() as conn:
            row = conn.fetchone(sql.FIND_USER, (issuer, subject))
            if row is None:
                user_id = _new_id()
                conn.execute(
                    sql.INSERT_USER,
                    (user_id, issuer, subject, email, display_name, now, now),
                )
            else:
                user_id = row["id"]
                conn.execute(
                    sql.TOUCH_USER,
                    (email or row["email"], display_name or row["display_name"],
                     now, user_id),
                )
            return _user(conn.fetchone(sql.GET_USER, (user_id,)))

    def get_user(self, user_id: str) -> Optional[dict]:
        with self._db.connect() as conn:
            return _user(conn.fetchone(sql.GET_USER, (user_id,)))

    def find_user(self, issuer: str, subject: str) -> Optional[dict]:
        with self._db.connect() as conn:
            return _user(conn.fetchone(sql.FIND_USER, (issuer, subject)))

    def list_users(self) -> list[dict]:
        with self._db.connect() as conn:
            return [_user(r) for r in conn.fetchall(sql.LIST_USERS)]

    # -- membership ----------------------------------------------------------
    def add_member(self, user_id: str, org_id: str,
                   roles: Sequence[str] = ()) -> dict:
        """Roles are per *membership*, not per user: being an operator at one
        customer must not make you one everywhere."""
        with self._db.connect() as conn:
            conn.execute(
                sql.insert_membership(self._db.dialect),
                (user_id, org_id, json.dumps(sorted(set(roles))), time.time()),
            )
            conn.execute(
                sql.UPSERT_MEMBERSHIP_UPDATE,
                (json.dumps(sorted(set(roles))), user_id, org_id),
            )
            return _membership(conn.fetchone(sql.GET_MEMBERSHIP, (user_id, org_id)))

    set_roles = add_member

    def remove_member(self, user_id: str, org_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(sql.DELETE_MEMBERSHIP, (user_id, org_id))

    def memberships(self, user_id: str) -> list[dict]:
        with self._db.connect() as conn:
            return [_membership(r)
                    for r in conn.fetchall(sql.LIST_MEMBERSHIPS, (user_id,))]

    def org_members(self, org_id: str) -> list[dict]:
        with self._db.connect() as conn:
            return [_membership(r)
                    for r in conn.fetchall(sql.LIST_ORG_MEMBERS, (org_id,))]

    # -- principals ----------------------------------------------------------
    def principal_for(self, user_id: str, org_id: Optional[str] = None,
                      source: str = "oidc") -> Optional[Principal]:
        """Assemble the acting identity for a person in one org.

        Returns None when the person is unknown, disabled, or not a member of
        the org they asked for — all three are "not authorized", and telling
        them apart at this layer would leak which users and orgs exist.
        """
        user = self.get_user(user_id)
        if user is None or user["disabled"]:
            return None
        member = self.memberships(user_id)
        if not member:
            return None
        if org_id is None:
            chosen = member[0]
        else:
            chosen = next((m for m in member if m["org_id"] == org_id), None)
            if chosen is None:
                return None
        org = self.get_org(chosen["org_id"])
        if org is None or org["disabled"]:
            return None
        return Principal(
            user_id=user["id"],
            tenant_id=chosen["org_id"],
            display_name=user["display_name"] or "",
            email=user["email"] or "",
            roles=frozenset(chosen["roles"]),
            source=source,
        )

    # -- API keys ------------------------------------------------------------
    def mint_api_key(self, user_id: str, org_id: str, name: str = "") -> tuple[str, dict]:
        """Create a key. The plaintext is returned once and never again."""
        key, prefix, key_hash = keymod.mint_key()
        key_id = _new_id()
        with self._db.connect() as conn:
            conn.execute(
                sql.INSERT_API_KEY,
                (key_id, prefix, key_hash, user_id, org_id, name, time.time()),
            )
        return key, {"id": key_id, "prefix": prefix, "user_id": user_id,
                     "org_id": org_id, "name": name}

    def authenticate_api_key(self, key: str) -> Optional[Principal]:
        """Resolve a presented key to a principal, or None.

        Finding the row is a lookup, not the decision: the prefix is not a
        secret, so the stored hash is still compared in constant time before
        anything is granted.
        """
        prefix, presented = keymod.split_key(key or "")
        if prefix is None:
            return None
        with self._db.connect() as conn:
            candidates = conn.fetchall(sql.FIND_API_KEYS_BY_PREFIX, (prefix,))
            match = next(
                (r for r in candidates if keymod.matches(presented, r["key_hash"])),
                None,
            )
            if match is None:
                return None
            conn.execute(sql.TOUCH_API_KEY, (time.time(), match["id"]))
            user_id, org_id = match["user_id"], match["org_id"]
        return self.principal_for(user_id, org_id, source="api_key")

    def revoke_api_key(self, key_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(sql.REVOKE_API_KEY, (time.time(), key_id))

    def list_api_keys(self, user_id: str) -> list[dict]:
        """Metadata only — there is no stored plaintext to return."""
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.LIST_API_KEYS, (user_id,))
        return [{"id": r["id"], "prefix": r["prefix"], "name": r["name"],
                 "org_id": r["org_id"], "created_at": r["created_at"],
                 "last_used_at": r["last_used_at"], "revoked_at": r["revoked_at"]}
                for r in rows]

    def close(self) -> None:
        self._db.close()


def _org(row: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"],
            "created_at": row["created_at"], "disabled": bool(row["disabled"])}


def _user(row: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if row is None:
        return None
    return {"id": row["id"], "issuer": row["issuer"], "subject": row["subject"],
            "email": row["email"], "display_name": row["display_name"],
            "created_at": row["created_at"], "last_login_at": row["last_login_at"],
            "disabled": bool(row["disabled"])}


def _membership(row: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if row is None:
        return None
    return {"user_id": row["user_id"], "org_id": row["org_id"],
            "roles": json.loads(row["roles"] or "[]"),
            "created_at": row["created_at"]}


_DEFAULT: Optional[AuthStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_auth_store() -> AuthStore:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = AuthStore()
        return _DEFAULT


def set_auth_store(store: Optional[AuthStore]) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = store
