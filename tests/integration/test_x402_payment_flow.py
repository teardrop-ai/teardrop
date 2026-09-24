# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Postgres integration coverage for x402 replay protection."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing.x402 as x402
from migrations.runner import apply_pending
from shared.db_pool import create_pool


@pytest.fixture
async def x402_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=5, name="integration-x402-payment")
    await apply_pending(pool)
    await pool.execute("TRUNCATE TABLE x402_payment_nonces")

    yield pool

    await pool.execute("TRUNCATE TABLE x402_payment_nonces")
    await pool.close()


def _configure_payment_boundary(monkeypatch, pool):
    payload = MagicMock(payload={})
    payload.model_dump_json.return_value = "signed-payment"
    requirement = MagicMock(scheme="exact")
    parser = MagicMock()
    parser.parse_payment_payload.return_value = payload
    server = MagicMock()
    server.verify_payment = AsyncMock(return_value=SimpleNamespace(is_valid=True, payer="0xabc"))
    server.settle_payment = AsyncMock(return_value=SimpleNamespace(success=True, transaction="0xsettled"))

    monkeypatch.setattr(x402, "_servers", [server])
    monkeypatch.setattr(x402, "_server", server)
    monkeypatch.setattr(x402, "_requirements_cache", [requirement])
    monkeypatch.setattr(x402, "_facilitator_failures", [0])
    monkeypatch.setattr(x402, "_facilitator_unhealthy_until", [0.0])
    monkeypatch.setattr(x402, "_rebuild_requirements_if_stale", AsyncMock())
    monkeypatch.setattr(x402, "_has_pool", lambda: True)
    monkeypatch.setattr(x402, "_get_pool", lambda: pool)
    return parser, payload, requirement, server


@pytest.mark.asyncio
async def test_concurrent_verified_header_has_one_winner_and_one_settlement(x402_pool, monkeypatch):
    parser, payload, requirement, server = _configure_payment_boundary(monkeypatch, x402_pool)
    payment_header = base64.b64encode(b"signed-payment").decode()

    with patch.dict("sys.modules", {"x402": parser}):
        results = await asyncio.gather(
            x402.verify_payment(payment_header),
            x402.verify_payment(payment_header),
        )
        nonce_hash = x402._payment_nonce_hash(payment_header)

    verified = [result for result in results if result.verified]
    rejected = [result for result in results if not result.verified]
    assert len(verified) == 1
    assert [result.error for result in rejected] == ["Payment already used. Sign a new payment authorization."]

    settled = await x402.settle_payment(verified[0])
    assert settled.settled is True
    assert settled.tx_hash == "0xsettled"
    server.settle_payment.assert_awaited_once_with(payload, requirement)

    assert (
        await x402_pool.fetchval(
            "SELECT COUNT(*) FROM x402_payment_nonces WHERE nonce_hash = $1",
            nonce_hash,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_released_unsettled_header_can_be_verified_again(x402_pool, monkeypatch):
    parser, _payload, _requirement, server = _configure_payment_boundary(monkeypatch, x402_pool)
    payment_header = base64.b64encode(b"released-payment").decode()

    with patch.dict("sys.modules", {"x402": parser}):
        first = await x402.verify_payment(payment_header)
        replay = await x402.verify_payment(payment_header)
        await x402.release_payment_nonce(payment_header)
        retry = await x402.verify_payment(payment_header)

    assert first.verified is True
    assert replay.verified is False
    assert replay.error == "Payment already used. Sign a new payment authorization."
    assert retry.verified is True
    assert server.verify_payment.await_count == 3
    assert await x402_pool.fetchval("SELECT COUNT(*) FROM x402_payment_nonces") == 1


@pytest.mark.asyncio
async def test_payer_spend_reservation_is_atomic_idempotent_and_releasable(x402_pool, monkeypatch):
    monkeypatch.setattr(x402, "_has_pool", lambda: True)
    monkeypatch.setattr(x402, "_get_pool", lambda: x402_pool)
    first_header = "payer-cap-payment-a"
    second_header = "payer-cap-payment-b"
    assert await x402._claim_payment_nonce(first_header) is True
    assert await x402._claim_payment_nonce(second_header) is True

    results = await asyncio.gather(
        x402.reserve_payer_spend(first_header, "0xAbC", 60, 100),
        x402.reserve_payer_spend(second_header, "0xabc", 60, 100),
    )

    assert sorted(results) == [False, True]
    winning_header = first_header if results[0] else second_header
    losing_header = second_header if results[0] else first_header
    assert await x402.reserve_payer_spend(winning_header, "0xABC", 60, 100) is True
    assert (
        await x402_pool.fetchval(
            "SELECT SUM(reserved_cost_usdc) FROM x402_payment_nonces WHERE LOWER(payer_address) = $1",
            "0xabc",
        )
        == 60
    )

    await x402.release_payment_nonce(winning_header)
    assert await x402.reserve_payer_spend(losing_header, "0xabc", 60, 100) is True


@pytest.mark.asyncio
async def test_reencoded_variant_of_same_payload_is_rejected(x402_pool, monkeypatch):
    """A re-encoded (whitespace/key-order) copy of one signed payload claims once."""
    from x402.schemas import PaymentPayload

    _configure_payment_boundary(monkeypatch, x402_pool)
    payload_dict = PaymentPayload(
        payload={"signature": "0xsigned"},
        accepted={
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "0x" + "83" * 20,
            "amount": "10000",
            "pay_to": "0x" + "f2" * 20,
            "max_timeout_seconds": 300,
        },
    ).model_dump(by_alias=True, exclude_none=True)
    compact = base64.b64encode(json.dumps(payload_dict).encode()).decode()
    spaced = base64.b64encode(json.dumps(payload_dict, indent=2).encode()).decode()

    first = await x402.verify_payment(compact)
    replay = await x402.verify_payment(spaced)

    assert first.verified is True
    assert replay.verified is False
    assert replay.error == "Payment already used. Sign a new payment authorization."
    assert await x402_pool.fetchval("SELECT COUNT(*) FROM x402_payment_nonces") == 1
