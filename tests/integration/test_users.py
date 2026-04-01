"""Integration tests for users.py CRUD against a real Postgres DB."""

from __future__ import annotations

import pytest

import users as user_module
from users import (
    Org,
    User,
    create_org,
    create_user,
    get_user_by_email,
    verify_secret,
)


@pytest.fixture(autouse=True)
def bind_pool(db_pool):
    """Point the global users._pool at our test pool for every test."""
    user_module._pool = db_pool
    yield
    user_module._pool = None


@pytest.mark.anyio
async def test_create_org(db_pool):
    org = await create_org("Test Org")
    assert isinstance(org, Org)
    assert org.name == "Test Org"
    assert len(org.id) == 36  # UUID


@pytest.mark.anyio
async def test_create_user(db_pool):
    org = await create_org("Org A")
    user = await create_user("alice@example.com", "s3cr3t!", org.id, role="user")
    assert isinstance(user, User)
    assert user.email == "alice@example.com"
    assert user.org_id == org.id
    assert user.role == "user"
    assert user.is_active is True


@pytest.mark.anyio
async def test_get_user_by_email(db_pool):
    org = await create_org("Org B")
    await create_user("bob@example.com", "pass123", org.id)
    user = await get_user_by_email("bob@example.com")
    assert user is not None
    assert user.email == "bob@example.com"


@pytest.mark.anyio
async def test_get_user_by_email_not_found(db_pool):
    result = await get_user_by_email("nobody@example.com")
    assert result is None


@pytest.mark.anyio
async def test_verify_secret_happy_path(db_pool):
    org = await create_org("Org C")
    user = await create_user("carol@example.com", "correcthorse", org.id)
    assert verify_secret("correcthorse", user.hashed_secret, user.salt) is True


@pytest.mark.anyio
async def test_verify_secret_wrong_password(db_pool):
    org = await create_org("Org D")
    user = await create_user("dave@example.com", "rightpass", org.id)
    assert verify_secret("wrongpass", user.hashed_secret, user.salt) is False


@pytest.mark.anyio
async def test_duplicate_email_raises(db_pool):
    import asyncpg

    org = await create_org("Org E")
    await create_user("dup@example.com", "pass", org.id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await create_user("dup@example.com", "other", org.id)


@pytest.mark.anyio
async def test_create_admin_user(db_pool):
    org = await create_org("Org F")
    user = await create_user("admin@example.com", "adminpass", org.id, role="admin")
    assert user.role == "admin"
