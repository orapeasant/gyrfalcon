"""Administration > Security — service accounts, OAuth tokens, secret store.

`gyrfalcon/security.py` holds the two credentials surfaces: OAuth2
client-credentials accounts other applications use to call *into* Gyrfalcon,
and secrets Gyrfalcon uses to call *out*. Both have one property worth pinning
hard: a secret value leaves the process exactly once, at the moment it is
minted, and is never recoverable from storage afterwards.
"""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point Gyrfalcon Home at a temp dir for the duration of one test.

    `get_gyrfalcon_home()` is `lru_cache(maxsize=1)`, so the env var alone is
    not enough — a stale cached Path would send writes to the real profile.
    """
    from gyrfalcon import gyrfalcon_constants as gc

    monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
    gc.get_gyrfalcon_home.cache_clear()
    yield tmp_path
    gc.get_gyrfalcon_home.cache_clear()


@pytest.fixture()
def sec(home):
    """The module, with its in-memory token table emptied between tests."""
    from gyrfalcon import security

    security._issued_tokens.clear()
    yield security
    security._issued_tokens.clear()


# ── Service accounts ─────────────────────────────────────────────────────────

class TestServiceAccountCreation:
    def test_create_returns_the_secret_once_and_never_stores_it(self, sec):
        account, secret = sec.create_service_account("partner", ["flow:read"])

        assert secret, "the plaintext secret is returned to the caller once"
        assert "client_secret" not in account
        assert "client_secret_hash" not in account, "the public record must not carry the hash"

        stored = sec.load_service_accounts()[0]
        assert stored["client_secret_hash"] != secret, "the secret is hashed at rest"
        assert secret not in json.dumps(stored), "no plaintext anywhere in the row"

    def test_two_accounts_get_distinct_credentials(self, sec):
        a1, s1 = sec.create_service_account("one", [])
        a2, s2 = sec.create_service_account("two", [])

        assert a1["client_id"] != a2["client_id"]
        assert s1 != s2

    def test_a_new_account_is_enabled_and_unused(self, sec):
        account, _ = sec.create_service_account("fresh", [])

        assert account["enabled"] is True
        assert account["last_used_at"] is None

    def test_accounts_persist_under_the_security_subfolder(self, sec, home):
        sec.create_service_account("persisted", [])

        path = home / "security" / "service_accounts.json"
        assert path.exists(), "grouped in its own subfolder, not flat in Gyrfalcon Home"
        assert json.loads(path.read_text())["service_accounts"][0]["name"] == "persisted"


class TestServiceAccountAuthentication:
    def test_the_right_secret_authenticates(self, sec):
        account, secret = sec.create_service_account("partner", [])

        assert sec.authenticate_service_account(account["client_id"], secret) is not None

    def test_a_wrong_secret_does_not(self, sec):
        account, _ = sec.create_service_account("partner", [])

        assert sec.authenticate_service_account(account["client_id"], "wrong") is None

    def test_an_unknown_client_id_does_not(self, sec):
        _, secret = sec.create_service_account("partner", [])

        assert sec.authenticate_service_account("sa_nonexistent", secret) is None

    def test_a_disabled_account_cannot_authenticate(self, sec):
        """Disabling is the kill switch — it must beat a still-valid secret."""
        account, secret = sec.create_service_account("partner", [])
        sec.set_service_account_enabled(account["id"], False)

        assert sec.authenticate_service_account(account["client_id"], secret) is None

    def test_re_enabling_restores_access(self, sec):
        account, secret = sec.create_service_account("partner", [])
        sec.set_service_account_enabled(account["id"], False)
        sec.set_service_account_enabled(account["id"], True)

        assert sec.authenticate_service_account(account["client_id"], secret) is not None

    def test_authentication_records_last_used(self, sec):
        account, secret = sec.create_service_account("partner", [])
        sec.authenticate_service_account(account["client_id"], secret)

        assert sec.load_service_accounts()[0]["last_used_at"] is not None


class TestSecretRotation:
    def test_rotation_invalidates_the_old_secret(self, sec):
        account, old = sec.create_service_account("partner", [])
        new = sec.rotate_service_account_secret(account["id"])

        assert new != old
        assert sec.authenticate_service_account(account["client_id"], old) is None
        assert sec.authenticate_service_account(account["client_id"], new) is not None

    def test_rotation_keeps_the_client_id(self, sec):
        """Only the secret rotates — a new client_id would be a new account,
        and every caller's config would have to change."""
        account, _ = sec.create_service_account("partner", [])
        sec.rotate_service_account_secret(account["id"])

        assert sec.load_service_accounts()[0]["client_id"] == account["client_id"]

    def test_rotating_an_unknown_account_returns_none(self, sec):
        assert sec.rotate_service_account_secret("nope") is None


class TestServiceAccountDeletion:
    def test_delete_removes_the_account(self, sec):
        account, secret = sec.create_service_account("partner", [])

        assert sec.delete_service_account(account["id"]) is True
        assert sec.load_service_accounts() == []
        assert sec.authenticate_service_account(account["client_id"], secret) is None

    def test_deleting_an_unknown_account_reports_false(self, sec):
        assert sec.delete_service_account("nope") is False

    def test_delete_leaves_other_accounts(self, sec):
        keep, _ = sec.create_service_account("keep", [])
        drop, _ = sec.create_service_account("drop", [])

        sec.delete_service_account(drop["id"])

        assert [a["id"] for a in sec.load_service_accounts()] == [keep["id"]]


# ── OAuth2 client-credentials tokens ─────────────────────────────────────────

class TestAccessTokens:
    def test_valid_credentials_mint_a_bearer_token(self, sec):
        account, secret = sec.create_service_account("partner", ["flow:read"])

        token = sec.issue_access_token(account["client_id"], secret)

        assert token["token_type"] == "Bearer"
        assert token["access_token"]
        assert token["expires_in"] > 0
        assert token["scope"] == "flow:read"

    def test_bad_credentials_mint_nothing(self, sec):
        account, _ = sec.create_service_account("partner", [])

        assert sec.issue_access_token(account["client_id"], "wrong") is None

    def test_a_disabled_account_cannot_mint(self, sec):
        account, secret = sec.create_service_account("partner", [])
        sec.set_service_account_enabled(account["id"], False)

        assert sec.issue_access_token(account["client_id"], secret) is None

    def test_an_issued_token_validates_and_carries_its_claims(self, sec):
        account, secret = sec.create_service_account("partner", ["flow:read"])
        token = sec.issue_access_token(account["client_id"], secret)

        claims = sec.validate_access_token(token["access_token"])
        assert claims["client_id"] == account["client_id"]
        assert claims["service_account_id"] == account["id"]
        assert claims["scopes"] == ["flow:read"]

    def test_an_unknown_token_does_not_validate(self, sec):
        assert sec.validate_access_token("not-a-real-token") is None

    def test_an_expired_token_does_not_validate_and_is_evicted(self, sec):
        account, secret = sec.create_service_account("partner", [])
        token = sec.issue_access_token(account["client_id"], secret)
        raw = token["access_token"]

        sec._issued_tokens[raw]["expires_at"] = time.time() - 1

        assert sec.validate_access_token(raw) is None
        assert raw not in sec._issued_tokens, "an expired token is dropped, not left to accumulate"

    def test_two_issues_produce_distinct_tokens(self, sec):
        account, secret = sec.create_service_account("partner", [])

        t1 = sec.issue_access_token(account["client_id"], secret)
        t2 = sec.issue_access_token(account["client_id"], secret)

        assert t1["access_token"] != t2["access_token"]
        assert sec.validate_access_token(t1["access_token"]) is not None, "re-issuing must not revoke the first"


# ── Secret store ─────────────────────────────────────────────────────────────

class TestSecretStore:
    def test_the_public_record_never_carries_the_value(self, sec):
        entry = sec.create_secret("stripe", "billing key", "sk_live_123")

        assert "value" not in entry
        assert all("value" not in sec.public_secret(s) for s in sec.load_secrets())

    def test_the_value_is_readable_server_side_by_name(self, sec):
        sec.create_secret("stripe", "billing key", "sk_live_123")

        assert sec.get_stored_secret("stripe") == "sk_live_123"

    def test_an_unknown_name_reads_none(self, sec):
        assert sec.get_stored_secret("nope") is None

    def test_update_replaces_the_value(self, sec):
        entry = sec.create_secret("stripe", "billing key", "old")

        sec.update_secret(entry["id"], value="new")

        assert sec.get_stored_secret("stripe") == "new"

    def test_update_can_change_the_description_without_touching_the_value(self, sec):
        """The edit form leaves `value` blank to mean "keep it" — passing None
        must not blank out a working secret."""
        entry = sec.create_secret("stripe", "old description", "sk_live_123")

        sec.update_secret(entry["id"], description="new description")

        assert sec.get_stored_secret("stripe") == "sk_live_123"
        assert sec.load_secrets()[0]["description"] == "new description"

    def test_update_stamps_updated_at(self, sec):
        entry = sec.create_secret("stripe", "", "v1")
        sec.update_secret(entry["id"], value="v2")

        assert sec.load_secrets()[0]["updated_at"] >= entry["created_at"]

    def test_updating_an_unknown_secret_returns_none(self, sec):
        assert sec.update_secret("nope", value="x") is None

    def test_delete_removes_it(self, sec):
        entry = sec.create_secret("stripe", "", "sk_live_123")

        assert sec.delete_secret(entry["id"]) is True
        assert sec.get_stored_secret("stripe") is None

    def test_deleting_an_unknown_secret_reports_false(self, sec):
        assert sec.delete_secret("nope") is False

    def test_secrets_persist_under_the_security_subfolder(self, sec, home):
        sec.create_secret("stripe", "", "sk_live_123")

        assert (home / "security" / "secrets.json").exists()
