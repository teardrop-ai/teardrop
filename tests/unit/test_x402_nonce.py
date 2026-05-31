"""Unit tests for the x402 concurrent-replay nonce guard.

Covers ``billing.x402._claim_payment_nonce`` (atomic INSERT … ON CONFLICT claim)
and ``billing.x402.cleanup_expired_payment_nonces`` (retention sweep). These
close the narrow window where two in-flight requests carrying the same signed
EIP-3009 payment header both verify and execute a paid tool before either
settles on-chain.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing.x402 as x402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _mock_pool(*, fetchval_return=None, fetchval_side_effect=None, execute_return="DELETE 0"):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=fetchval_return, side_effect=fetchval_side_effect)
    pool.execute = AsyncMock(return_value=execute_return)
    return pool


async def test_first_claim_succeeds():
    """A never-seen header is claimed and the caller may proceed."""
    pool = _mock_pool(fetchval_return="deadbeefhash")
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        assert await x402._claim_payment_nonce("X-PAYMENT-header") is True
    pool.fetchval.assert_awaited_once()


async def test_replay_claim_rejected():
    """A header already present (ON CONFLICT → no RETURNING row) is rejected."""
    pool = _mock_pool(fetchval_return=None)
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        assert await x402._claim_payment_nonce("X-PAYMENT-header") is False


async def test_same_header_claimed_once_across_two_calls():
    """First call wins, second (same header) loses — concurrent-replay guard."""
    pool = _mock_pool(fetchval_side_effect=["hash", None])
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        first = await x402._claim_payment_nonce("dup-header")
        second = await x402._claim_payment_nonce("dup-header")
    assert first is True
    assert second is False


async def test_fail_open_when_no_pool():
    """No bound pool → fail open (allow) so a DB outage can't halt paid traffic."""
    with patch.object(x402, "_has_pool", return_value=False):
        assert await x402._claim_payment_nonce("header") is True


async def test_fail_open_on_db_error():
    """A DB exception during claim fails open; the chain remains the backstop."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=Exception("connection reset"))
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        assert await x402._claim_payment_nonce("header") is True


async def test_distinct_headers_both_claim():
    """Two different headers each claim independently."""
    pool = _mock_pool(fetchval_side_effect=["hash-a", "hash-b"])
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        assert await x402._claim_payment_nonce("header-a") is True
        assert await x402._claim_payment_nonce("header-b") is True


async def test_cleanup_returns_deleted_count():
    pool = _mock_pool(execute_return="DELETE 7")
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        deleted = await x402.cleanup_expired_payment_nonces()
    assert deleted == 7
    pool.execute.assert_awaited_once()


async def test_cleanup_no_pool_returns_zero():
    with patch.object(x402, "_has_pool", return_value=False):
        assert await x402.cleanup_expired_payment_nonces() == 0


async def test_cleanup_unparsable_result_returns_zero():
    pool = _mock_pool(execute_return="DELETE")
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        assert await x402.cleanup_expired_payment_nonces() == 0
