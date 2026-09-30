# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from x402.schemas import VerifyResponse  # noqa: F401  # loaded before tests patch sys.modules["x402"]

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


_BASE = "eip155:8453"
_FACILITATOR_ADDRESS = "0x" + "f" * 40


def _facilitator_client(*kinds: tuple[str, dict | None]):
    from x402.schemas import SupportedKind, SupportedResponse

    response = SupportedResponse(
        kinds=[SupportedKind(x402_version=2, scheme=scheme, network=_BASE, extra=extra) for scheme, extra in kinds]
    )
    return SimpleNamespace(get_supported=lambda: response)


async def _init_upto_with(monkeypatch, clients: dict):
    settings = _init_settings()
    settings.x402_scheme = "upto"
    monkeypatch.setattr(x402, "get_settings", lambda: settings)
    monkeypatch.setattr(x402, "get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(x402, "async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr(x402, "_bind_pool", MagicMock())
    monkeypatch.setattr(x402, "_servers", [])
    with patch("x402.http.HTTPFacilitatorClient", side_effect=lambda config: clients[config.url]):
        await x402.init_billing(MagicMock())


@pytest.mark.anyio
async def test_init_upto_skips_facilitator_without_facilitator_address(monkeypatch):
    compliant = _facilitator_client(("exact", None), ("upto", {"facilitatorAddress": _FACILITATOR_ADDRESS}))
    clients = {
        "https://one.example": _facilitator_client(("exact", None), ("upto", None)),
        "https://two.example": compliant,
    }

    await _init_upto_with(monkeypatch, clients)

    assert len(x402._servers) == 1
    upto = [req for req in x402.get_payment_requirements() if req.scheme == "upto"]
    assert len(upto) == 2
    assert all(req.extra["facilitatorAddress"] == _FACILITATOR_ADDRESS for req in upto)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kinds",
    [
        pytest.param((("exact", None),), id="upto-not-advertised"),
        pytest.param((("exact", None), ("upto", None)), id="upto-missing-facilitator-address"),
    ],
)
async def test_init_upto_fails_closed_when_no_facilitator_is_compliant(monkeypatch, kinds):
    clients = {url: _facilitator_client(*kinds) for url in ("https://one.example", "https://two.example")}

    with pytest.raises(RuntimeError, match="No x402 facilitator could be initialized for scheme=upto"):
        await _init_upto_with(monkeypatch, clients)


@pytest.mark.anyio
async def test_usdc_topup_stays_exact_eip3009_when_scheme_is_upto(monkeypatch):
    # Mirrors Dexter's Base /supported, which advertises permit2 on exact.
    dexter = _facilitator_client(
        ("exact", {"assetTransferMethod": "permit2", "minPaymentAmountAtomic": "1509"}),
        ("upto", {"facilitatorAddress": _FACILITATOR_ADDRESS}),
    )
    await _init_upto_with(monkeypatch, {"https://one.example": dexter, "https://two.example": dexter})

    topup = x402.build_usdc_topup_requirements(1_000_000)

    assert len(topup) == 2
    assert all(req.scheme == "exact" and req.amount == "1000000" for req in topup)
    assert all("assetTransferMethod" not in (req.extra or {}) for req in topup)
    assert all("facilitatorAddress" not in (req.extra or {}) for req in topup)


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


_CDP_REJECTION = ValueError(
    'Facilitator verify failed (400): {"invalidMessage":"contract call failed: execution reverted",'
    '"invalidReason":"invalid_payload","isValid":false,"payer":"0xabc"}'
)


@pytest.mark.anyio
async def test_verify_4xx_rejection_is_a_result_not_an_outage(monkeypatch):
    first = MagicMock(verify_payment=AsyncMock(side_effect=_CDP_REJECTION))
    second = MagicMock(verify_payment=AsyncMock(return_value=SimpleNamespace(is_valid=True, payer="0xabc")))
    _requirement, parser = _verification_context(monkeypatch, [first, second])

    with patch.dict("sys.modules", {"x402": parser}):
        result = await x402.verify_payment(base64.b64encode(b"payment").decode())

    assert result.verified is False
    assert result.error == "Payment verification failed: invalid_payload"
    second.verify_payment.assert_not_awaited()
    assert x402._facilitator_unhealthy_until[0] == 0.0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ValueError('Facilitator verify failed (400): {"isValid":true,"payer":"0xabc"}'), id="4xx-claims-valid"),
        pytest.param(ValueError("Facilitator verify failed (400): not json"), id="4xx-unparseable"),
        pytest.param(ValueError('Facilitator verify failed (503): {"isValid":false}'), id="5xx"),
    ],
)
async def test_verify_non_rejection_errors_still_fail_over(monkeypatch, error):
    first = MagicMock(verify_payment=AsyncMock(side_effect=error))
    second = MagicMock(verify_payment=AsyncMock(return_value=SimpleNamespace(is_valid=True, payer="0xabc")))
    _requirement, parser = _verification_context(monkeypatch, [first, second])

    with patch.dict("sys.modules", {"x402": parser}):
        result = await x402.verify_payment(base64.b64encode(b"payment").decode())

    assert result.verified is True
    assert result.facilitator_index == 1
    assert x402._facilitator_unhealthy_until[0] > 0.0


def test_cdp_facilitator_client_is_authenticated():
    config = {"url": "https://api.cdp.coinbase.com/platform/v2/x402", "create_headers": lambda: {}}
    with (
        patch("cdp.x402.create_facilitator_config", return_value=config) as create_config,
        patch("x402.http.HTTPFacilitatorClient") as client_factory,
    ):
        x402.build_facilitator_client("https://api.cdp.coinbase.com/platform/v2/x402", "key-id", "key-secret")

    create_config.assert_called_once_with("key-id", "key-secret")
    client_factory.assert_called_once_with(config)


def test_cdp_facilitator_requires_api_key():
    with pytest.raises(RuntimeError, match="CDP_API_KEY_ID"):
        x402.build_facilitator_client("https://api.cdp.coinbase.com/platform/v2/x402")


@pytest.mark.anyio
async def test_init_skips_cdp_facilitator_without_api_key(monkeypatch):
    settings = _init_settings()
    settings.x402_facilitator_urls = ["https://api.cdp.coinbase.com/platform/v2/x402", "https://two.example"]
    settings.cdp_api_key_id = ""
    settings.cdp_api_key_secret = ""
    fallback = MagicMock()
    fallback.build_payment_requirements.side_effect = lambda config: [config]
    monkeypatch.setattr(x402, "get_settings", lambda: settings)
    monkeypatch.setattr(x402, "get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(x402, "async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr(x402, "_bind_pool", MagicMock())
    monkeypatch.setattr(x402, "_servers", [])

    with (
        patch("x402.x402ResourceServer", MagicMock(return_value=fallback)),
        patch("x402.http.HTTPFacilitatorClient") as client_factory,
    ):
        await x402.init_billing(MagicMock())

    assert x402._servers == [fallback]
    assert client_factory.call_args.args[0].url == "https://two.example"


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
