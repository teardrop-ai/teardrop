# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Postgres integration coverage for x402 replay protection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
    payload = MagicMock()
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

    verified = [result for result in results if result.verified]
    rejected = [result for result in results if not result.verified]
    assert len(verified) == 1
    assert [result.error for result in rejected] == ["Payment already used. Sign a new payment authorization."]

    settled = await x402.settle_payment(verified[0])
    assert settled.settled is True
    assert settled.tx_hash == "0xsettled"
    server.settle_payment.assert_awaited_once_with(payload, requirement)

    nonce_hash = hashlib.sha256(payment_header.encode()).hexdigest()
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
