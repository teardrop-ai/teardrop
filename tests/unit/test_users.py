"""Unit tests for users.py — DB functions mocked via pool MagicMock."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import users as users_module
from users import Org, User, _hash_secret, verify_secret

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
        from users import create_org

        pool = _pool()
        with patch.object(users_module, "_pool", pool):
            org = await create_org("ACME")
        assert isinstance(org, Org)
        assert org.name == "ACME"
        pool.execute.assert_called_once()

    async def test_db_error_propagates(self):
        from users import create_org

        pool = _pool()
        pool.execute = AsyncMock(side_effect=Exception("duplicate key"))
        with patch.object(users_module, "_pool", pool):
            with pytest.raises(Exception, match="duplicate key"):
                await create_org("duplicate-name")


# ─── create_user ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestCreateUser:
    async def test_returns_user_model(self):
        from users import create_user

        pool = _pool()
        with patch.object(users_module, "_pool", pool):
            user = await create_user("test@test.com", "secret", "org-1", "user")
        assert isinstance(user, User)
        assert user.email == "test@test.com"
        assert user.role == "user"
        # Plaintext secret must NOT appear in stored fields
        assert user.hashed_secret != "secret"

    async def test_admin_role(self):
        from users import create_user

        pool = _pool()
        with patch.object(users_module, "_pool", pool):
            user = await create_user("admin@test.com", "pw", "org-1", "admin")
        assert user.role == "admin"


# ─── get_user_by_email ────────────────────────────────────────────────────────


@pytest.mark.anyio
class TestGetUserByEmail:
    async def test_returns_user_when_found_and_active(self):
        from users import get_user_by_email

        row = {
            "id": "u-1",
            "email": "test@test.com",
            "org_id": "org-1",
            "hashed_secret": "abc",
            "salt": "def",
            "role": "user",
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module, "_pool", pool):
            user = await get_user_by_email("test@test.com")
        assert user is not None
        assert user.id == "u-1"

    async def test_returns_none_when_inactive(self):
        from users import get_user_by_email

        row = {
            "id": "u-2",
            "email": "inactive@test.com",
            "org_id": "org-1",
            "hashed_secret": "abc",
            "salt": "def",
            "role": "user",
            "is_active": False,
            "created_at": datetime.now(timezone.utc),
        }
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=row)
        with patch.object(users_module, "_pool", pool):
            user = await get_user_by_email("inactive@test.com")
        assert user is None

    async def test_returns_none_when_not_found(self):
        from users import get_user_by_email

        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)
        with patch.object(users_module, "_pool", pool):
            user = await get_user_by_email("nobody@test.com")
        assert user is None


# ─── init & close helpers ─────────────────────────────────────────────────────


@pytest.mark.anyio
class TestInitAndClose:
    async def test_close_user_db_clears_pool(self):
        from users import close_user_db

        with patch.object(users_module, "_pool", MagicMock()):
            await close_user_db()
        assert users_module._pool is None

    async def test_get_pool_raises_when_uninitialised(self):
        from users import _get_pool

        with patch.object(users_module, "_pool", None):
            with pytest.raises(RuntimeError, match="not initialised"):
                _get_pool()
