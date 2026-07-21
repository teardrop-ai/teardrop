"""Unit tests for users.py — DB functions mocked via pool MagicMock."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.users as users_module
from teardrop.users import Org, User, _hash_secret, verify_secret

# ─── Pure helpers ─────────────────────────────────────────────────────────────


class TestHashAndVerifySecret:
    def test_round_trip(self):
        hashed, salt = _hash_secret("my-secret")
        assert verify_secret("my-secret", hashed, salt)

    def test_wrong_secret_fails(self):
        hashed, salt = _hash_secret("correct-horse")
        assert not verify_secret("battery-staple", hashed, salt)

    def test_deterministic_with_same_salt(self):
        import os

        salt = os.urandom(32)
        h1, s1 = _hash_secret("pw", salt)
        h2, s2 = _hash_secret("pw", salt)
        assert h1 == h2
        assert s1 == s2


# ─── Pool mock helper ─────────────────────────────────────────────────────────


def _pool():
    pool = MagicMock()
    pool.fetchrow = AsyncMock()
    pool.execute = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


# ─── create_org ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCreateOrg:
    async def test_returns_org_model(self):
        from teardrop.users import create_org

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            org = await create_org("ACME", acquisition_source="founder_email")
        assert isinstance(org, Org)
        assert org.name == "ACME"
        assert org.slug == "acme"
        assert org.acquisition_source == "founder_email"
        pool.execute.assert_called_once()
        assert "slug" in pool.execute.call_args.args[0]

    async def test_reserves_platform_catalog_namespace(self):
        from teardrop.users import create_org

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            org = await create_org("Platform")

        assert org.slug == "platform-org"

    async def test_db_error_propagates(self):
        from teardrop.users import create_org

        pool = _pool()
        pool.execute = AsyncMock(side_effect=Exception("duplicate key"))
        with patch.object(users_module.base, "_pool", pool):
            with pytest.raises(Exception, match="duplicate key"):
                await create_org("duplicate-name")


# ─── create_user ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCreateUser:
    async def test_returns_user_model(self):
        from teardrop.users import create_user

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            user = await create_user("test@test.com", "secret", "org-1", "user")
        assert isinstance(user, User)
        assert user.email == "test@test.com"
        assert user.role == "user"
        # Plaintext secret must NOT appear in stored fields
        assert user.hashed_secret != "secret"

    async def test_admin_role(self):
        from teardrop.users import create_user

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            user = await create_user("admin@test.com", "pw", "org-1", "admin")
        assert user.role == "admin"


# ─── get_user_by_email ────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetUserByEmail:
    async def test_returns_user_when_found_and_active(self):
        from teardrop.users import get_user_by_email

        row = {
            "id": "u-1",
            "email": "test@test.com",
            "org_id": "org-1",
            "hashed_secret": "abc",
            "salt": "def",
            "role": "user",
            "is_active": True,
            "is_verified": True,
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            user = await get_user_by_email("test@test.com")
        assert user is not None
        assert user.id == "u-1"

    async def test_returns_none_when_inactive(self):
        from teardrop.users import get_user_by_email

        row = {
            "id": "u-2",
            "email": "inactive@test.com",
            "org_id": "org-1",
            "hashed_secret": "abc",
            "salt": "def",
            "role": "user",
            "is_active": False,
            "is_verified": True,
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            user = await get_user_by_email("inactive@test.com")
        assert user is None

    async def test_returns_none_when_not_found(self):
        from teardrop.users import get_user_by_email

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            user = await get_user_by_email("nobody@test.com")
        assert user is None


# ─── init & close helpers ─────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitAndClose:
    async def test_close_user_db_clears_pool(self):
        from teardrop.users import close_user_db

        with patch.object(users_module.base, "_pool", MagicMock()):
            await close_user_db()
        assert users_module.base._pool is None

    async def test_get_pool_raises_when_uninitialised(self):
        from teardrop.users import _get_pool

        with patch.object(users_module.base, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                _get_pool()

    async def test_init_user_db_sets_pool(self):
        from teardrop.users import init_user_db

        pool = _pool()
        with patch.object(users_module.base, "_pool", None):
            await init_user_db(pool)
            # Assert inside the patch context before restoration
            assert users_module.base._pool is pool
        assert pool.execute.call_count >= 6  # tables + indexes


# ─── get_org_by_id ────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetOrgById:
    async def test_returns_org_when_found(self):
        from teardrop.users import get_org_by_id

        row = {
            "id": "org-1",
            "name": "ACME",
            "acquisition_source": "founder_email",
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            org = await get_org_by_id("org-1")
        assert org is not None
        assert org.id == "org-1"
        assert org.name == "ACME"
        assert org.acquisition_source == "founder_email"

    async def test_returns_none_when_not_found(self):
        from teardrop.users import get_org_by_id

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            org = await get_org_by_id("missing")
        assert org is None


# ─── get_org_by_name ──────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetOrgByName:
    async def test_returns_org_when_found(self):
        from teardrop.users import get_org_by_name

        row = {
            "id": "org-2",
            "name": "Globex",
            "acquisition_source": "directory_listing",
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            org = await get_org_by_name("Globex")
        assert org is not None
        assert org.name == "Globex"
        assert org.acquisition_source == "directory_listing"

    async def test_returns_none_when_not_found(self):
        from teardrop.users import get_org_by_name

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            org = await get_org_by_name("unknown")
        assert org is None


# ─── get_user_by_org_id ───────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetUserByOrgId:
    async def test_returns_user_when_found(self):
        from teardrop.users import get_user_by_org_id

        row = {
            "id": "u-10",
            "email": "u@test.com",
            "org_id": "org-1",
            "hashed_secret": "h",
            "salt": "s",
            "role": "user",
            "is_active": True,
            "is_verified": True,
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            user = await get_user_by_org_id("org-1")
        assert user is not None
        assert user.org_id == "org-1"

    async def test_returns_none_when_not_found(self):
        from teardrop.users import get_user_by_org_id

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            user = await get_user_by_org_id("empty-org")
        assert user is None


# ─── client credentials ───────────────────────────────────────────────────────


def _make_cred_row():
    return {
        "client_id": "cid-1",
        "org_id": "org-1",
        "hashed_secret": "h",
        "salt": "s",
        "created_at": datetime.now(timezone.utc),
    }


@pytest.mark.anyio
class TestClientCredentials:
    async def test_create_returns_cred_and_plaintext(self):
        from teardrop.users import create_client_credential

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            cred, plaintext = await create_client_credential("org-1")
        assert cred.org_id == "org-1"
        assert len(plaintext) > 20
        pool.execute.assert_called_once()

    async def test_get_by_id_found(self):
        from teardrop.users import get_client_credential_by_id

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=_make_cred_row())
        with patch.object(users_module.base, "_pool", pool):
            cred = await get_client_credential_by_id("cid-1")
        assert cred is not None
        assert cred.client_id == "cid-1"

    async def test_get_by_id_not_found(self):
        from teardrop.users import get_client_credential_by_id

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            cred = await get_client_credential_by_id("missing")
        assert cred is None

    async def test_list_returns_creds(self):
        from teardrop.users import list_org_client_credentials

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[_make_cred_row()])
        with patch.object(users_module.base, "_pool", pool):
            creds = await list_org_client_credentials("org-1")
        assert len(creds) == 1
        assert creds[0].org_id == "org-1"

    async def test_list_returns_empty(self):
        from teardrop.users import list_org_client_credentials

        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])
        with patch.object(users_module.base, "_pool", pool):
            creds = await list_org_client_credentials("org-1")
        assert creds == []

    async def test_delete_executes(self):
        from teardrop.users import delete_org_client_credentials

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            await delete_org_client_credentials("org-1")
        pool.execute.assert_called_once()


# ─── register_org_and_user ────────────────────────────────────────────────────


def _make_transactional_pool():
    """Return (pool, conn) mocks that support `async with pool.acquire() as conn`."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool, conn


@pytest.mark.anyio
class TestRegisterOrgAndUser:
    async def test_creates_org_and_user(self):
        from teardrop.users import register_org_and_user

        pool, conn = _make_transactional_pool()
        with patch.object(users_module.base, "_pool", pool):
            org, user = await register_org_and_user(
                "MyOrg",
                "user@test.com",
                "pass123",
                acquisition_source="founder_email",
            )
        assert org.name == "MyOrg"
        assert org.acquisition_source == "founder_email"
        assert user.email == "user@test.com"
        assert user.is_verified is False
        assert conn.execute.call_count == 2  # INSERT orgs + INSERT users

    async def test_db_error_propagates(self):
        from teardrop.users import register_org_and_user

        pool, conn = _make_transactional_pool()
        conn.execute = AsyncMock(side_effect=Exception("unique violation"))
        with patch.object(users_module.base, "_pool", pool):
            with pytest.raises(Exception, match="unique violation"):
                await register_org_and_user("dup", "dup@test.com", "pw")


# ─── email verification tokens ────────────────────────────────────────────────


@pytest.mark.anyio
class TestVerificationTokens:
    async def test_create_returns_token_string(self):
        from teardrop.users import create_verification_token

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            token = await create_verification_token("user-1")
        assert isinstance(token, str)
        assert len(token) > 10
        pool.execute.assert_called_once()

    async def test_consume_valid_token_returns_user_id(self):
        from teardrop.users import consume_verification_token

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        from datetime import timedelta

        conn.fetchrow = AsyncMock(
            return_value={
                "user_id": "u-42",
                "expires_at": now + timedelta(hours=1),
                "used": False,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            uid = await consume_verification_token("valid-tok")
        assert uid == "u-42"
        conn.execute.assert_called_once()  # UPDATE SET used=TRUE

    async def test_consume_expired_token_returns_none(self):
        from teardrop.users import consume_verification_token

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(
            return_value={
                "user_id": "u-1",
                "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                "used": False,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            uid = await consume_verification_token("expired")
        assert uid is None

    async def test_consume_already_used_returns_none(self):
        from teardrop.users import consume_verification_token

        pool, conn = _make_transactional_pool()
        from datetime import timedelta

        conn.fetchrow = AsyncMock(
            return_value={
                "user_id": "u-1",
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                "used": True,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            uid = await consume_verification_token("used-tok")
        assert uid is None

    async def test_consume_missing_token_returns_none(self):
        from teardrop.users import consume_verification_token

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            uid = await consume_verification_token("missing")
        assert uid is None

    async def test_mark_user_verified(self):
        from teardrop.users import mark_user_verified

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            await mark_user_verified("user-1")
        pool.execute.assert_called_once()


# ─── atomic verify + onboarding-credit outbox enqueue ─────────────────────────


@pytest.mark.anyio
class TestVerifyUserAndEnqueueOnboardingCredit:
    async def test_invalid_token_returns_none(self):
        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            user_id, org_id, already_verified = await verify_user_and_enqueue_onboarding_credit("missing", True, 500_000)
        assert user_id is None
        assert org_id is None
        assert already_verified is False

    async def test_expired_token_returns_none(self):
        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(
            return_value={
                "user_id": "u-1",
                "expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                "used": False,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            user_id, org_id, _ = await verify_user_and_enqueue_onboarding_credit("expired", True, 500_000)
        assert user_id is None
        assert org_id is None

    async def test_already_used_token_returns_none(self):
        from datetime import timedelta

        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(
            return_value={
                "user_id": "u-1",
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                "used": True,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            user_id, org_id, _ = await verify_user_and_enqueue_onboarding_credit("used-tok", True, 500_000)
        assert user_id is None
        assert org_id is None

    async def test_valid_token_marks_verified_and_enqueues_outbox_when_enabled(self):
        from datetime import timedelta

        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"user_id": "u-1", "expires_at": now + timedelta(hours=1), "used": False},
                {"org_id": "org-1", "is_verified": False},
            ]
        )
        with patch.object(users_module.base, "_pool", pool):
            user_id, org_id, already_verified = await verify_user_and_enqueue_onboarding_credit("valid-tok", True, 500_000)
        assert user_id == "u-1"
        assert org_id == "org-1"
        assert already_verified is False
        # token consume UPDATE, user verified UPDATE, outbox INSERT
        assert conn.execute.call_count == 3
        outbox_sql = conn.execute.call_args_list[-1].args[0]
        assert "org_onboarding_credit_outbox" in outbox_sql

    async def test_valid_token_skips_outbox_when_disabled(self):
        from datetime import timedelta

        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"user_id": "u-1", "expires_at": now + timedelta(hours=1), "used": False},
                {"org_id": "org-1", "is_verified": False},
            ]
        )
        with patch.object(users_module.base, "_pool", pool):
            user_id, org_id, _ = await verify_user_and_enqueue_onboarding_credit("valid-tok", False, 500_000)
        assert user_id == "u-1"
        assert org_id == "org-1"
        # token consume UPDATE + user verified UPDATE only, no outbox insert
        assert conn.execute.call_count == 2

    async def test_already_verified_user_flag_is_reported(self):
        from datetime import timedelta

        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"user_id": "u-1", "expires_at": now + timedelta(hours=1), "used": False},
                {"org_id": "org-1", "is_verified": True},
            ]
        )
        with patch.object(users_module.base, "_pool", pool):
            _, _, already_verified = await verify_user_and_enqueue_onboarding_credit("valid-tok", False, 500_000)
        assert already_verified is True

    async def test_missing_user_row_returns_none(self):
        from datetime import timedelta

        from teardrop.users import verify_user_and_enqueue_onboarding_credit

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"user_id": "u-1", "expires_at": now + timedelta(hours=1), "used": False},
                None,
            ]
        )
        with patch.object(users_module.base, "_pool", pool):
            user_id, org_id, _ = await verify_user_and_enqueue_onboarding_credit("valid-tok", True, 500_000)
        assert user_id is None
        assert org_id is None


# ─── org invites ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestOrgInvites:
    async def test_create_returns_invite(self):
        from teardrop.users import create_org_invite

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            invite = await create_org_invite("org-1", "admin-id", email="new@test.com")
        assert invite.org_id == "org-1"
        assert invite.email == "new@test.com"
        assert invite.used is False
        pool.execute.assert_called_once()

    async def test_get_valid_invite(self):
        from datetime import timedelta

        from teardrop.users import get_org_invite

        pool = _pool()
        now = datetime.now(timezone.utc)
        row = {
            "token": "tok-1",
            "org_id": "org-1",
            "email": "x@test.com",
            "role": "user",
            "invited_by": "admin",
            "created_at": now,
            "expires_at": now + timedelta(hours=72),
            "used": False,
        }
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            invite = await get_org_invite("tok-1")
        assert invite is not None
        assert invite.token == "tok-1"

    async def test_get_missing_invite_returns_none(self):
        from teardrop.users import get_org_invite

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            invite = await get_org_invite("nope")
        assert invite is None

    async def test_get_used_invite_returns_none(self):
        from datetime import timedelta

        from teardrop.users import get_org_invite

        pool = _pool()
        now = datetime.now(timezone.utc)
        row = {
            "token": "t",
            "org_id": "o",
            "email": None,
            "role": "user",
            "invited_by": "a",
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
            "used": True,
        }
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module.base, "_pool", pool):
            invite = await get_org_invite("t")
        assert invite is None

    async def test_consume_invite_success(self):
        from datetime import timedelta

        from teardrop.users import consume_org_invite

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(return_value={"used": False, "expires_at": now + timedelta(hours=1)})
        with patch.object(users_module.base, "_pool", pool):
            result = await consume_org_invite("tok-1")
        assert result is True
        conn.execute.assert_called_once()

    async def test_consume_invite_already_used(self):
        from datetime import timedelta

        from teardrop.users import consume_org_invite

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(return_value={"used": True, "expires_at": now + timedelta(hours=1)})
        with patch.object(users_module.base, "_pool", pool):
            result = await consume_org_invite("used-tok")
        assert result is False

    async def test_consume_invite_not_found(self):
        from teardrop.users import consume_org_invite

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            result = await consume_org_invite("missing")
        assert result is False


# ─── refresh tokens ───────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestRefreshTokens:
    async def test_create_refresh_token_returns_string(self):
        from teardrop.users import create_refresh_token

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            token = await create_refresh_token("u-1", "org-1", "password", {}, 30)
        assert isinstance(token, str)
        assert len(token) > 10
        pool.execute.assert_called_once()

    async def test_rotate_success(self):
        from datetime import timedelta

        from teardrop.users import rotate_refresh_token

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            return_value={
                "token": "old-tok",
                "user_id": "u-1",
                "org_id": "org-1",
                "auth_method": "password",
                "extra_claims": {},
                "created_at": now,
                "expires_at": now + timedelta(days=30),
                "revoked": False,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            result = await rotate_refresh_token("old-tok", 30)
        assert result is not None
        record, new_token = result
        assert record.token == "old-tok"
        assert isinstance(new_token, str)

    async def test_rotate_revoked_returns_none(self):
        from datetime import timedelta

        from teardrop.users import rotate_refresh_token

        pool, conn = _make_transactional_pool()
        now = datetime.now(timezone.utc)
        conn.fetchrow = AsyncMock(
            return_value={
                "token": "t",
                "user_id": "u",
                "org_id": "o",
                "auth_method": "pw",
                "extra_claims": {},
                "created_at": now,
                "expires_at": now + timedelta(days=1),
                "revoked": True,
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            result = await rotate_refresh_token("revoked-tok", 30)
        assert result is None

    async def test_rotate_not_found_returns_none(self):
        from teardrop.users import rotate_refresh_token

        pool, conn = _make_transactional_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            result = await rotate_refresh_token("missing", 30)
        assert result is None

    async def test_revoke_refresh_token(self):
        from teardrop.users import revoke_refresh_token

        pool = _pool()
        with patch.object(users_module.base, "_pool", pool):
            await revoke_refresh_token("some-tok")
        pool.execute.assert_called_once()

    async def test_cleanup_expired_returns_count(self):
        from teardrop.users import cleanup_expired_refresh_tokens

        pool = _pool()
        pool.execute = AsyncMock(return_value="DELETE 5")
        with patch.object(users_module.base, "_pool", pool):
            count = await cleanup_expired_refresh_tokens()
        assert count == 5

    async def test_cleanup_handles_malformed_result(self):
        from teardrop.users import cleanup_expired_refresh_tokens

        pool = _pool()
        pool.execute = AsyncMock(return_value="OK")
        with patch.object(users_module.base, "_pool", pool):
            count = await cleanup_expired_refresh_tokens()
        assert count == 0

    async def test_get_refresh_token_successor_found(self):
        from teardrop.users import get_refresh_token_successor

        pool = _pool()
        now = datetime.now(timezone.utc)
        from datetime import timedelta

        pool.fetchrow = AsyncMock(
            return_value={
                "token": "new-tok",
                "user_id": "u-1",
                "org_id": "org-1",
                "auth_method": "password",
                "extra_claims": {},
                "created_at": now,
                "expires_at": now + timedelta(days=30),
            }
        )
        with patch.object(users_module.base, "_pool", pool):
            rec = await get_refresh_token_successor("old-tok")
        assert rec is not None
        assert rec.token == "new-tok"

    async def test_get_refresh_token_successor_not_found(self):
        from teardrop.users import get_refresh_token_successor

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module.base, "_pool", pool):
            rec = await get_refresh_token_successor("no-successor")
        assert rec is None
