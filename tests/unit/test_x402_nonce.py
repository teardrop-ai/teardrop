# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for the x402 concurrent-replay nonce guard.

Covers ``billing.x402._claim_payment_nonce`` (atomic INSERT … ON CONFLICT claim)
and ``billing.x402.cleanup_expired_payment_nonces`` (retention sweep). These
close the narrow window where two in-flight requests carrying the same signed
EIP-3009 payment header both verify and execute a paid tool before either
settles on-chain.
"""

from __future__ import annotations

import base64
import hashlib
import json
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


# ── Canonical payload hashing ────────────────────────────────────────────────


def _encode_payment(payload: dict) -> str:
    from x402.schemas import PaymentPayload

    return base64.b64encode(PaymentPayload(**payload).model_dump_json().encode()).decode()


def _sample_payload() -> dict:
    return {
        "x402_version": 2,
        "payload": {"signature": "0x" + "ab" * 32},
        "accepted": {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "0x" + "83" * 20,
            "amount": "10000",
            "pay_to": "0x" + "f2" * 20,
            "max_timeout_seconds": 300,
            "extra": {"name": "USD Coin", "version": "2"},
        },
    }


def test_canonical_hash_is_stable_across_reencoding():
    """Whitespace/key-order/padding variants of one payload share a nonce hash."""
    from x402.schemas import PaymentPayload

    payload = PaymentPayload(**_sample_payload())
    compact = base64.b64encode(payload.model_dump_json().encode()).decode()
    spaced = base64.b64encode(json.dumps(payload.model_dump(by_alias=True), indent=2).encode()).decode()
    reordered = base64.b64encode(json.dumps(dict(reversed(list(payload.model_dump(by_alias=True).items())))).encode()).decode()

    assert x402._payment_nonce_hash(compact) == x402._payment_nonce_hash(spaced)
    assert x402._payment_nonce_hash(compact) == x402._payment_nonce_hash(reordered)


def test_canonical_hash_differs_for_distinct_payloads():
    first = _encode_payment(_sample_payload())
    second = _encode_payment({**_sample_payload(), "payload": {"signature": "0x" + "cd" * 32}})
    assert x402._payment_nonce_hash(first) != x402._payment_nonce_hash(second)


def test_canonical_hash_falls_back_to_raw_header_on_malformed_input():
    malformed = base64.b64encode(b"not-a-payment-payload").decode()

    assert x402._payment_nonce_hash(malformed) == hashlib.sha256(malformed.encode()).hexdigest()
    assert x402._payment_nonce_hash("!!!not base64!!!") == hashlib.sha256(b"!!!not base64!!!").hexdigest()


def _eip3009_payload(nonce: str = "0x" + "0a" * 32, **overrides) -> dict:
    authorization = {
        "from": "0x" + "Ab" * 20,
        "to": "0x" + "f2" * 20,
        "value": "10000",
        "validAfter": "0",
        "validBefore": "9999999999",
        "nonce": nonce,
    }
    return {**_sample_payload(), "payload": {"signature": "0x" + "ab" * 65, "authorization": authorization}, **overrides}


def test_authorization_hash_ignores_unsigned_fields():
    """Repackaging one signed EVM authorization cannot mint a fresh replay claim."""
    base = x402._payment_nonce_hash(_encode_payment(_eip3009_payload()))
    upper_nonce = _eip3009_payload(nonce="0x" + "0A" * 32)
    junk_inner = _eip3009_payload()
    junk_inner["payload"] = {**junk_inner["payload"], "junk": 1, "signature": "0x" + "AB" * 65}
    variants = [
        _eip3009_payload(extensions={"bazaar": {"x": 1}}),
        _eip3009_payload(resource={"url": "https://other.example/tools/mcp"}),
        upper_nonce,
        junk_inner,
    ]

    assert all(x402._payment_nonce_hash(_encode_payment(variant)) == base for variant in variants)


def test_authorization_hash_differs_per_nonce_and_supports_permit2():
    first = x402._payment_nonce_hash(_encode_payment(_eip3009_payload()))
    second = x402._payment_nonce_hash(_encode_payment(_eip3009_payload(nonce="0x" + "0b" * 32)))
    permit2 = _sample_payload()
    permit2["payload"] = {"signature": "0x01", "permit2Authorization": {"from": "0x" + "ab" * 20, "nonce": "42"}}
    permit2_padded = _sample_payload()
    permit2_padded["payload"] = {"signature": "0x02", "permit2Authorization": {"from": "0x" + "AB" * 20, "nonce": "0042"}}

    assert first != second
    assert x402._payment_nonce_hash(_encode_payment(permit2)) == x402._payment_nonce_hash(_encode_payment(permit2_padded))
    assert x402._payment_nonce_hash(_encode_payment(permit2)) != first


async def test_reencoded_replay_of_same_payload_is_rejected():
    """Whitespace/key-order re-encodings of one signed payload claim once."""
    from x402.schemas import PaymentPayload

    payload = PaymentPayload(**_sample_payload())
    compact = base64.b64encode(payload.model_dump_json().encode()).decode()
    spaced = base64.b64encode(json.dumps(payload.model_dump(by_alias=True), indent=2).encode()).decode()

    pool = _mock_pool(fetchval_side_effect=["hash", None])
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        assert await x402._claim_payment_nonce(compact) is True
        assert await x402._claim_payment_nonce(spaced) is False
    assert pool.fetchval.await_args.args[0].count("$1") == 1


async def test_release_and_reserve_share_canonical_hash():
    from x402.schemas import PaymentPayload

    payload = PaymentPayload(**_sample_payload())
    compact = base64.b64encode(payload.model_dump_json().encode()).decode()
    spaced = base64.b64encode(json.dumps(payload.model_dump(by_alias=True), indent=2).encode()).decode()

    pool = _mock_pool()
    with (
        patch.object(x402, "_has_pool", return_value=True),
        patch.object(x402, "_get_pool", return_value=pool),
    ):
        await x402.release_payment_nonce(spaced)
    assert pool.execute.await_args.args[0] == "DELETE FROM x402_payment_nonces WHERE nonce_hash = $1"
    assert pool.execute.await_args.args[1] == x402._payment_nonce_hash(compact)


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


async def test_payer_spend_reservation_fails_closed_without_pool():
    with patch.object(x402, "_has_pool", return_value=False):
        assert await x402.reserve_payer_spend("header", "0xabc", 10_000, 5_000_000) is False


@pytest.mark.parametrize(
    ("payer", "amount", "limit"),
    [("", 1, 5_000_000), ("0xabc", -1, 5_000_000), ("0xabc", 1, 0)],
)
async def test_payer_spend_reservation_rejects_invalid_boundaries(payer, amount, limit):
    with patch.object(x402, "_has_pool", return_value=True):
        assert await x402.reserve_payer_spend("header", payer, amount, limit) is False
