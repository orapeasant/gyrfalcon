"""The dashboard API's authentication boundary, over real HTTP.

Why this exists at the HTTP level rather than as unit tests: a service
account's OAuth token is accepted in `_verify_token`, *before*
`_bind_request_principal` runs — and an earlier version of this feature put
the check in the wrong one of those two functions. Every unit test passed and
the feature was still broken, because with identity disabled (the default)
`_verify_token` returns 401 before the principal resolver is ever reached.
Only a request exercises that ordering.

Marked `slow_import`-ish by nature: importing `web_server` builds the whole
FastAPI app once per session.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi.testclient", reason="needs fastapi's TestClient")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """One app + client for the module; Gyrfalcon Home isolated to a temp dir."""
    import os

    from gyrfalcon import gyrfalcon_constants as gc

    home = tmp_path_factory.mktemp("gyrfalcon_home")
    os.environ["GYRFALCON_HOME"] = str(home)
    gc.get_gyrfalcon_home.cache_clear()

    from fastapi.testclient import TestClient

    from gyrfalcon_cli import web_server

    with TestClient(web_server.app) as c:
        yield c, web_server

    gc.get_gyrfalcon_home.cache_clear()


@pytest.fixture()
def session_headers(client):
    _, web_server = client
    return {"X-Gyrfalcon-Session-Token": web_server._session_token}


@pytest.fixture()
def account(client):
    """A live service account plus its one-time secret."""
    from gyrfalcon import security

    security._issued_tokens.clear()
    acct, secret = security.create_service_account("test-integration", ["flow:read"])
    yield acct, secret, security
    security.delete_service_account(acct["id"])
    security._issued_tokens.clear()


#: A representative protected endpoint: cheap, read-only, and gated the same
#: way every other dashboard route is.
PROTECTED = "/api/applications"


class TestSessionTokenAuth:
    def test_no_credential_is_rejected(self, client):
        c, _ = client
        assert c.get(PROTECTED).status_code == 401

    def test_a_wrong_session_token_is_rejected(self, client):
        c, _ = client
        r = c.get(PROTECTED, headers={"X-Gyrfalcon-Session-Token": "nope"})
        assert r.status_code == 401

    def test_the_session_token_is_accepted(self, client, session_headers):
        c, _ = client
        assert c.get(PROTECTED, headers=session_headers).status_code == 200

    def test_status_is_authenticated_too(self, client, session_headers):
        """`/api/status` is *not* an open liveness probe — it calls
        `_verify_token` like everything else, and it discloses Gyrfalcon Home,
        config paths and the PID, so gating it is the right call. Pinned
        because "the health endpoint is public" is a reasonable-sounding
        assumption that would be a small information leak here.
        """
        c, _ = client
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers=session_headers).status_code == 200


class TestOAuthTokenEndpoint:
    def test_valid_credentials_mint_a_token(self, client, account):
        c, _ = client
        acct, secret, _ = account

        r = c.post("/api/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": acct["client_id"],
            "client_secret": secret,
        })

        assert r.status_code == 200
        assert r.json()["token_type"] == "Bearer"
        assert r.json()["access_token"]

    def test_the_token_endpoint_needs_no_session_token(self, client, account):
        """The whole point: an external app authenticates on its own, holding
        only its client credentials."""
        c, _ = client
        acct, secret, _ = account

        r = c.post("/api/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": acct["client_id"],
            "client_secret": secret,
        })
        assert r.status_code == 200

    def test_a_wrong_secret_is_rejected(self, client, account):
        c, _ = client
        acct, _secret, _ = account

        r = c.post("/api/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": acct["client_id"],
            "client_secret": "wrong",
        })
        assert r.status_code == 401

    def test_an_unsupported_grant_type_is_rejected(self, client, account):
        c, _ = client
        acct, secret, _ = account

        r = c.post("/api/oauth/token", json={
            "grant_type": "password",
            "client_id": acct["client_id"],
            "client_secret": secret,
        })
        assert r.status_code == 400


class TestOAuthBearerAuthorizesTheApi:
    """The regression this file exists for."""

    def _token(self, c, acct, secret) -> str:
        r = c.post("/api/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": acct["client_id"],
            "client_secret": secret,
        })
        return r.json()["access_token"]

    def test_a_service_account_token_is_accepted_with_identity_disabled(self, client, account):
        """The bug: `_verify_token` short-circuits to 401 on a missing session
        token *before* `_bind_request_principal` runs, so a bearer check that
        lives only in the principal resolver never executes in the default
        (identity-disabled) configuration."""
        c, _ = client
        acct, secret, _ = account
        token = self._token(c, acct, secret)

        r = c.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})

        assert r.status_code == 200, "an OAuth bearer must authorize the API on its own"

    def test_a_garbage_bearer_is_rejected(self, client):
        c, _ = client
        r = c.get(PROTECTED, headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401

    def test_a_malformed_authorization_header_is_rejected(self, client):
        c, _ = client
        r = c.get(PROTECTED, headers={"Authorization": "Basic abc123"})
        assert r.status_code == 401

    def test_an_expired_token_is_rejected(self, client, account):
        c, _ = client
        acct, secret, security = account
        token = self._token(c, acct, secret)
        security._issued_tokens[token]["expires_at"] = time.time() - 1

        r = c.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})

        assert r.status_code == 401

    def test_deleting_the_account_does_not_retract_a_live_token(self, client, account):
        """Documents current behaviour, which is a real gap rather than a
        decision: `validate_access_token` checks only expiry, so a token keeps
        working for up to its full TTL after the account behind it is deleted
        or disabled. Revocation is therefore eventual, bounded by
        `_ACCESS_TOKEN_TTL_SECONDS` (1 hour), not immediate.

        If that is not acceptable, the fix is to re-check the account inside
        `validate_access_token`. Change this test with it.
        """
        c, _ = client
        acct, secret, security = account
        token = self._token(c, acct, secret)

        security.set_service_account_enabled(acct["id"], False)

        r = c.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, "known gap: disabling does not revoke issued tokens"

        # The credential itself is dead immediately, so no *new* token can be minted.
        assert security.issue_access_token(acct["client_id"], secret) is None


class TestServiceAccountEndpoints:
    def test_listing_requires_a_credential(self, client):
        c, _ = client
        assert c.get("/api/security/service-accounts").status_code == 401

    def test_the_created_secret_is_returned_once_and_never_listed(self, client, session_headers):
        c, _ = client
        created = c.post(
            "/api/security/service-accounts",
            headers=session_headers,
            json={"name": "once-only", "scopes": []},
        ).json()

        assert created["client_secret"], "returned at creation"

        listed = c.get("/api/security/service-accounts", headers=session_headers).json()
        row = next(a for a in listed["service_accounts"] if a["id"] == created["id"])
        assert "client_secret" not in row
        assert "client_secret_hash" not in row, "the hash must not leak over the API either"

        c.delete(f"/api/security/service-accounts/{created['id']}", headers=session_headers)

    def test_a_stored_secret_value_is_never_returned(self, client, session_headers):
        c, _ = client
        created = c.post(
            "/api/security/secrets",
            headers=session_headers,
            json={"name": "api_key", "description": "d", "value": "sk_live_123"},
        ).json()

        assert "value" not in created
        listed = c.get("/api/security/secrets", headers=session_headers).json()
        assert all("value" not in s for s in listed["secrets"])

        c.delete(f"/api/security/secrets/{created['id']}", headers=session_headers)
