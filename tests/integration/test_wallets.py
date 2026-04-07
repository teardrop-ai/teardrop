"""Integration tests for wallets.py CRUD and SIWE nonce lifecycle."""

from __future__ import annotations

import pytest

import users as user_module
import wallets as wallet_module
from users import create_org, create_user
from wallets import (
    Wallet,
    consume_nonce,
    create_nonce,
    create_wallet,
    delete_wallet,
    get_wallet_by_address,
    get_wallets_by_user,
)

# EIP-55 checksummed addresses for testing
_ADDR_1 = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
_ADDR_2 = "0xAbcd1234AbcD1234AbcD1234AbcD1234aBcD1234"


@pytest.fixture(autouse=True)
def bind_pools(db_pool):
    """Bind both module-level _pool globals to the test pool."""
    user_module._pool = db_pool
    wallet_module._pool = db_pool
    yield
    user_module._pool = None
    wallet_module._pool = None


@pytest.fixture
async def test_user(db_pool):
    org = await create_org("Wallet Org")
    return await create_user("wallet_user@example.com", "pass", org.id)


@pytest.mark.anyio
async def test_create_wallet(test_user):
    wallet = await create_wallet(
        address=_ADDR_1,
        chain_id=1,
        user_id=test_user.id,
        org_id=test_user.org_id,
    )
    assert isinstance(wallet, Wallet)
    assert wallet.address == _ADDR_1
    assert wallet.chain_id == 1


@pytest.mark.anyio
async def test_get_wallet_by_address(test_user):
    await create_wallet(_ADDR_1, 1, test_user.id, test_user.org_id)
    found = await get_wallet_by_address(_ADDR_1, chain_id=1)
    assert found is not None
    assert found.address == _ADDR_1


@pytest.mark.anyio
async def test_get_wallet_by_address_not_found(test_user):
    result = await get_wallet_by_address("0x000000000000000000000000000000000000dEaD")
    assert result is None


@pytest.mark.anyio
async def test_get_wallets_by_user(test_user):
    await create_wallet(_ADDR_1, 1, test_user.id, test_user.org_id)
    await create_wallet(_ADDR_2, 1, test_user.id, test_user.org_id)
    wallets = await get_wallets_by_user(test_user.id)
    assert len(wallets) == 2


@pytest.mark.anyio
async def test_delete_wallet(test_user):
    wallet = await create_wallet(_ADDR_1, 1, test_user.id, test_user.org_id)
    deleted = await delete_wallet(wallet.id, test_user.id)
    assert deleted is True
    found = await get_wallet_by_address(_ADDR_1, chain_id=1)
    assert found is None


@pytest.mark.anyio
async def test_delete_wallet_wrong_user(test_user):
    wallet = await create_wallet(_ADDR_1, 1, test_user.id, test_user.org_id)
    deleted = await delete_wallet(wallet.id, "wrong-user-id")
    assert deleted is False


@pytest.mark.anyio
async def test_unique_constraint_address_chain(test_user):
    import asyncpg

    await create_wallet(_ADDR_1, 1, test_user.id, test_user.org_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await create_wallet(_ADDR_1, 1, test_user.id, test_user.org_id)


@pytest.mark.anyio
async def test_create_nonce_returns_string():
    nonce = await create_nonce()
    assert isinstance(nonce, str)
    assert len(nonce) > 0


@pytest.mark.anyio
async def test_consume_nonce_happy_path():
    nonce = await create_nonce()
    result = await consume_nonce(nonce, ttl_seconds=300)
    assert result is True


@pytest.mark.anyio
async def test_consume_nonce_replay_rejected():
    nonce = await create_nonce()
    first = await consume_nonce(nonce, ttl_seconds=300)
    second = await consume_nonce(nonce, ttl_seconds=300)
    assert first is True
    assert second is False


@pytest.mark.anyio
async def test_consume_nonce_expired():
    nonce = await create_nonce()
    # TTL of 0 seconds — immediately expired.
    result = await consume_nonce(nonce, ttl_seconds=0)
    assert result is False


@pytest.mark.anyio
async def test_consume_nonexistent_nonce():
    result = await consume_nonce("this-nonce-does-not-exist", ttl_seconds=300)
    assert result is False
