# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing as billing_facade
import billing.x402 as x402
from billing.models import BillingResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _verification_context(monkeypatch, servers):
    requirement = MagicMock(scheme="exact")
    parser = MagicMock()
    parser.parse_payment_payload.return_value = MagicMock()
    monkeypatch.setattr(x402, "_servers", servers)
    monkeypatch.setattr(x402, "_server", servers[0])
    monkeypatch.setattr(x402, "_requirements_cache", [requirement])
    monkeypatch.setattr(x402, "_facilitator_failures", [0] * len(servers))
    monkeypatch.setattr(x402, "_facilitator_unhealthy_until", [0.0] * len(servers))
    monkeypatch.setattr(x402, "_rebuild_requirements_if_stale", AsyncMock())
    monkeypatch.setattr(x402, "_claim_payment_nonce", AsyncMock(return_value=True))
    return requirement, parser


def _init_settings():
    return SimpleNamespace(
        billing_enabled=True,
        x402_facilitator_url="https://legacy.example",
        x402_facilitator_urls=["https://one.example", "https://two.example"],
        x402_pay_to_address="",
        x402_treasury_addresses=["0x" + "a" * 40, "0x" + "b" * 40],
        x402_network="eip155:8453",
        x402_scheme="exact",
        x402_run_price="$0.01",
        x402_upto_max_amount="$0.50",
    )


@pytest.mark.anyio
async def test_init_constructs_one_server_per_facilitator(monkeypatch):
    settings = _init_settings()
    first = MagicMock()
    second = MagicMock()
    first.build_payment_requirements.side_effect = lambda config: [config]
    server_factory = MagicMock(side_effect=[first, second])
    monkeypatch.setattr(x402, "get_settings", lambda: settings)
    monkeypatch.setattr(x402, "get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(x402, "async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr(x402, "_bind_pool", MagicMock())
    monkeypatch.setattr(x402, "_servers", [])

    with (
        patch("x402.x402ResourceServer", server_factory),
        patch("x402.http.HTTPFacilitatorClient") as client_factory,
    ):
        await x402.init_billing(MagicMock())

    assert x402._servers == [first, second]
    assert x402._server is first
    assert client_factory.call_count == 2
    assert first.initialize.call_count == 1
    assert second.initialize.call_count == 1
    assert len(x402.get_payment_requirements()) == 2


@pytest.mark.anyio
async def test_init_fails_closed_when_all_facilitators_are_blocked(monkeypatch):
    monkeypatch.setattr(x402, "get_settings", _init_settings)
    monkeypatch.setattr(x402, "async_validate_url", AsyncMock(return_value="blocked"))
    monkeypatch.setattr(x402, "_bind_pool", MagicMock())

    with pytest.raises(RuntimeError, match="No x402 facilitator"):
        await x402.init_billing(MagicMock())


@pytest.mark.anyio
async def test_verify_fails_over_on_transport_error(monkeypatch):
    first = MagicMock(verify_payment=AsyncMock(side_effect=OSError("secret transport detail")))
    second = MagicMock(verify_payment=AsyncMock(return_value=SimpleNamespace(is_valid=True, payer="0xabc")))
    requirement, parser = _verification_context(monkeypatch, [first, second])

    with patch.dict("sys.modules", {"x402": parser}):
        result = await x402.verify_payment(base64.b64encode(b"payment").decode())

    assert result.verified is True
    assert result.facilitator_index == 1
    first.verify_payment.assert_awaited_once_with(parser.parse_payment_payload.return_value, requirement)
    second.verify_payment.assert_awaited_once_with(parser.parse_payment_payload.return_value, requirement)


@pytest.mark.anyio
async def test_verify_skips_facilitator_during_cooldown(monkeypatch):
    first = MagicMock(verify_payment=AsyncMock())
    second = MagicMock(verify_payment=AsyncMock(return_value=SimpleNamespace(is_valid=True, payer="0xabc")))
    _requirement, parser = _verification_context(monkeypatch, [first, second])
    monkeypatch.setattr(x402, "_facilitator_unhealthy_until", [float("inf"), 0.0])

    with patch.dict("sys.modules", {"x402": parser}):
        result = await x402.verify_payment(base64.b64encode(b"payment").decode())

    assert result.facilitator_index == 1
    first.verify_payment.assert_not_awaited()


@pytest.mark.anyio
async def test_settlement_is_pinned_to_verifying_facilitator(monkeypatch):
    first = MagicMock(settle_payment=AsyncMock())
    second = MagicMock(settle_payment=AsyncMock(return_value=SimpleNamespace(success=True, transaction="0xtx")))
    monkeypatch.setattr(x402, "_servers", [first, second])
    requirement = SimpleNamespace(amount="10000")
    verified = BillingResult(
        verified=True,
        payment_payload=object(),
        payment_requirements=requirement,
        facilitator_index=1,
    )

    result = await x402.settle_payment(verified)

    assert result.settled is True
    assert result.tx_hash == "0xtx"
    first.settle_payment.assert_not_awaited()
    second.settle_payment.assert_awaited_once_with(verified.payment_payload, requirement)


@pytest.mark.anyio
async def test_settlement_does_not_fall_back_to_another_facilitator(monkeypatch):
    first = MagicMock(settle_payment=AsyncMock())
    second = MagicMock(settle_payment=AsyncMock(side_effect=OSError("unavailable")))
    monkeypatch.setattr(x402, "_servers", [first, second])
    verified = BillingResult(
        verified=True,
        payment_payload=object(),
        payment_requirements=SimpleNamespace(amount="10000"),
        facilitator_index=1,
    )

    result = await x402.settle_payment(verified)

    assert result.settled is False
    first.settle_payment.assert_not_awaited()
    second.settle_payment.assert_awaited_once()


def test_facilitator_affinity_is_internal():
    result = BillingResult(facilitator_index=2)

    assert result.model_dump() == BillingResult().model_dump()


def test_build_requirements_advertises_each_treasury():
    resource_config = MagicMock(side_effect=lambda **values: SimpleNamespace(**values))
    fake_x402 = MagicMock(ResourceConfig=resource_config)
    server = MagicMock()
    server.build_payment_requirements.side_effect = lambda config: [config]
    settings = SimpleNamespace(x402_network="eip155:8453", x402_scheme="exact")
    treasuries = ["0x" + "a" * 40, "0x" + "b" * 40]

    with patch.dict("sys.modules", {"x402": fake_x402}):
        exact, upto, combined = x402._build_requirements(server, settings, treasuries, "$0.01")

    assert [requirement.pay_to for requirement in exact] == treasuries
    assert upto is None
    assert combined == exact


@pytest.mark.anyio
async def test_treasury_change_rebuilds_cached_requirements(monkeypatch):
    treasuries = ["0x" + "a" * 40, "0x" + "b" * 40]
    settings = SimpleNamespace(
        x402_facilitator_url="https://facilitator.example",
        x402_facilitator_urls=[],
        x402_pay_to_address="",
        x402_treasury_addresses=treasuries,
        x402_network="eip155:8453",
        x402_scheme="exact",
    )
    resource_config = MagicMock(side_effect=lambda **values: SimpleNamespace(**values))
    fake_x402 = MagicMock(ResourceConfig=resource_config)
    server = MagicMock()
    server.build_payment_requirements.side_effect = lambda config: [config]
    monkeypatch.setattr(billing_facade, "_server", server)
    monkeypatch.setattr(billing_facade, "_servers", [server])
    monkeypatch.setattr(billing_facade, "_requirements_cache", None)
    monkeypatch.setattr(billing_facade, "_last_requirements_price_usdc", 10_000)
    monkeypatch.setattr(billing_facade, "_last_requirements_topology", None)
    monkeypatch.setattr(billing_facade, "get_settings", lambda: settings)
    monkeypatch.setattr(
        billing_facade,
        "get_live_pricing",
        AsyncMock(return_value=SimpleNamespace(run_price_usdc=10_000)),
    )

    with patch.dict("sys.modules", {"x402": fake_x402}):
        await billing_facade._rebuild_requirements_if_stale()

    assert [requirement.pay_to for requirement in billing_facade.get_payment_requirements()] == treasuries
    assert billing_facade._last_requirements_topology == (
        (treasuries[0], treasuries[1]),
        ("https://facilitator.example",),
    )
