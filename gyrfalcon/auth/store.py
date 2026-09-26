"""The identity store — orgs, people, membership, and API keys.

Spec: §17.11 step 8.

Shares the flow database, dialect layer and migration counter (see
`db/schema.py`), so a deployment configures one DSN rather than two and
cannot end up with half a schema.

Everything here runs *before* a principal exists — it is what produces one —
so none of it is tenant-scoped and none of it should be reachable from
ordinary request handling. `auth/` is that boundary.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import base64
import os
import threading
import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from gyrfalcon.auth import keys as keymod
from gyrfalcon.db import open_database, resolve_target, sql
from gyrfalcon.db.migrations import ensure_schema
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
    @staticmethod
    def _password_hash(password: str, salt: Optional[bytes] = None) -> str:
        salt = salt or os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
        return "pbkdf2_sha256$310000$%s$%s" % (
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    @staticmethod
    def _password_matches(password: str, encoded: str) -> bool:
        try:
            algorithm, rounds, salt_text, expected_text = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256" or int(rounds) != 310_000:
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    def ensure_default_admin(self) -> None:
        """Create the initial admin once; never reset an existing password."""
        if self.local_credential("admin") is not None:
            return
        if self.get_org("local") is None:
            try:
                self.create_org("Default", org_id="local")
            except Exception:
                if self.get_org("local") is None:
                    raise
        user = self.find_user("local-password", "admin")
        if user is None:
            now = time.time()
            user_id = _new_id()
            with self._db.connect() as conn:
                conn.execute(sql.INSERT_USER, (user_id, "local-password", "admin", "", "Administrator", now, None))
            user = self.get_user(user_id)
        self.add_member(user["id"], "local", roles=("admin", "operator"))
        try:
            with self._db.connect() as conn:
                conn.execute(sql.INSERT_LOCAL_CREDENTIAL,
                             ("admin", user["id"], self._password_hash("admin"), 1, time.time()))
        except Exception:
            # A concurrent first request may have inserted it.
            if self.local_credential("admin") is None:
                raise

    def local_credential(self, username: str) -> Optional[dict]:
        with self._db.connect() as conn:
            row = conn.fetchone(sql.FIND_LOCAL_CREDENTIAL, (username.strip().lower(),))
        return dict(row) if row is not None else None

    def local_credential_for_user(self, user_id: str) -> Optional[dict]:
        with self._db.connect() as conn:
            row = conn.fetchone(sql.GET_LOCAL_CREDENTIAL_BY_USER, (user_id,))
        return dict(row) if row is not None else None

    def authenticate_local(self, username: str, password: str) -> tuple[Optional[Principal], bool]:
        credential = self.local_credential(username)
        if credential is None or not self._password_matches(password, credential["password_hash"]):
            # Keep an unknown username near the cost of a failed password check.
            self._password_matches(password, self._password_hash("", b"gyrfalcon-dummy!!"))
            return None, False
        with self._db.connect() as conn:
            conn.execute(sql.TOUCH_LOCAL_LOGIN, (time.time(), credential["user_id"]))
        principal = self.principal_for(credential["user_id"], source="password")
        return principal, bool(credential["must_change"])

    def change_local_password(self, user_id: str, current_password: str, password: str) -> bool:
        with self._db.connect() as conn:
            row = conn.fetchone(sql.GET_LOCAL_CREDENTIAL_BY_USER, (user_id,))
        if row is None or not self._password_matches(current_password, row["password_hash"]):
            return False
        with self._db.connect() as conn:
            conn.execute(sql.UPDATE_LOCAL_CREDENTIAL,
                         (self._password_hash(password), time.time(), user_id))
        return True

    def reset_local_password(self, tenant_id: str, user_id: str, password: str) -> bool:
        with self._db.connect() as conn:
            if conn.fetchone(sql.GET_TENANT_USER, (user_id, tenant_id)) is None:
                return False
            cur = conn.execute(sql.RESET_LOCAL_CREDENTIAL,
                               (self._password_hash(password), time.time(), user_id))
            return bool(getattr(cur, "rowcount", 0))

    def has_local_credentials(self) -> bool:
        with self._db.connect() as conn:
            return conn.fetchone(sql.HAS_LOCAL_CREDENTIALS) is not None

    # -- tenant users and groups --------------------------------------------
    def list_tenant_users(self, tenant_id: str) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.LIST_TENANT_USERS, (tenant_id,))
        users = []
        for row in rows:
            user = dict(row)
            user["roles"] = json.loads(user.get("roles") or "[]")
            user["group_ids"] = self.user_group_ids(tenant_id, user["id"])
            user["disabled"] = bool(user["disabled"])
            users.append(user)
        return users

    def create_local_user(self, tenant_id: str, username: str, password: str,
                          display_name: str, email: str, roles: Sequence[str]) -> dict:
        normalized = username.strip().lower()
        if not normalized or any(ch.isspace() for ch in normalized):
            raise ValueError("Username must be non-empty and contain no spaces")
        if self.local_credential(normalized) is not None:
            raise ValueError("That username is already in use")
        now = time.time()
        user_id = _new_id()
        with self._db.connect() as conn:
            conn.execute(sql.INSERT_USER, (user_id, "local-password", normalized,
                                           email.strip(), display_name.strip() or normalized,
                                           now, None))
            conn.execute(sql.insert_membership(self._db.dialect),
                         (user_id, tenant_id, json.dumps(sorted(set(roles))), now))
            conn.execute(sql.INSERT_LOCAL_CREDENTIAL,
                         (normalized, user_id, self._password_hash(password), 0, now))
        return next(user for user in self.list_tenant_users(tenant_id) if user["id"] == user_id)

    def update_tenant_user(self, tenant_id: str, user_id: str, *, display_name: str,
                           email: str, disabled: bool, roles: Sequence[str]) -> bool:
        with self._db.connect() as conn:
            if conn.fetchone(sql.GET_TENANT_USER, (user_id, tenant_id)) is None:
                return False
            conn.execute(sql.UPDATE_USER_PROFILE,
                         (display_name, email, 1 if disabled else 0, user_id))
            conn.execute(sql.UPDATE_USER_ROLES,
                         (json.dumps(sorted(set(roles))), user_id, tenant_id))
        return True

    def list_groups(self, tenant_id: str) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.LIST_AUTH_GROUPS, (tenant_id,))
        groups = [dict(row) for row in rows]
        for group in groups:
            group["user_ids"] = self.group_user_ids(tenant_id, group["id"])
        return groups

    def create_group(self, tenant_id: str, name: str, description: str = "") -> dict:
        group_id = _new_id()
        with self._db.connect() as conn:
            conn.execute(sql.INSERT_AUTH_GROUP,
                         (group_id, tenant_id, name.strip(), description.strip(), time.time()))
        return next(group for group in self.list_groups(tenant_id) if group["id"] == group_id)

    def update_group(self, tenant_id: str, group_id: str, name: str, description: str) -> bool:
        with self._db.connect() as conn:
            cur = conn.execute(sql.UPDATE_AUTH_GROUP, (name.strip(), description.strip(), tenant_id, group_id))
            return bool(getattr(cur, "rowcount", 0))

    def delete_group(self, tenant_id: str, group_id: str) -> bool:
        with self._db.connect() as conn:
            conn.execute(sql.DELETE_AUTH_GROUP_MEMBERS, (tenant_id, group_id))
            cur = conn.execute(sql.DELETE_AUTH_GROUP, (tenant_id, group_id))
            return bool(getattr(cur, "rowcount", 0))

    def user_group_ids(self, tenant_id: str, user_id: str) -> list[str]:
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.LIST_USER_GROUPS, (tenant_id, user_id))
        return [row["group_id"] for row in rows]

    def group_user_ids(self, tenant_id: str, group_id: str) -> list[str]:
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.LIST_GROUP_MEMBERS, (tenant_id, group_id))
        return [row["user_id"] for row in rows]

    def set_user_groups(self, tenant_id: str, user_id: str, group_ids: Sequence[str]) -> bool:
        wanted = sorted(set(group_ids))
        with self._db.connect() as conn:
            if conn.fetchone(sql.GET_TENANT_USER, (user_id, tenant_id)) is None:
                return False
            known = {row["id"] for row in conn.fetchall(sql.LIST_AUTH_GROUPS, (tenant_id,))}
            if not set(wanted).issubset(known):
                raise ValueError("One or more groups do not exist in this organization")
            conn.execute(sql.DELETE_USER_GROUPS, (tenant_id, user_id))
            for group_id in wanted:
                conn.execute(sql.INSERT_GROUP_MEMBER,
                             (tenant_id, group_id, user_id, time.time()))
        return True

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
