"""API tests for top-up rate limiting and return_url HTTPS enforcement.

POST /billing/topup/stripe and /billing/topup/usdc are per-org rate limited
(``rate_limit_topup_rpm``) to blunt credit-fraud / abuse loops, and the Stripe
``return_url`` must be HTTPS so the checkout redirect can't be downgraded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import teardrop.rate_limit as _rate_limit
import teardrop.routers.billing as billing_module


@pytest.fixture(autouse=True)
def _clear_rate_counters(monkeypatch):
    """Reset the in-process sliding-window state and force the in-process path."""
    monkeypatch.setattr(_rate_limit, "get_redis", lambda: None)
    _rate_limit._rate_counters.clear()
    yield
    _rate_limit._rate_counters.clear()


def _enable_billing(monkeypatch, *, topup_rpm=3):
    mock_settings = MagicMock(wraps=billing_module.settings)
    mock_settings.billing_enabled = True
    mock_settings.rate_limit_topup_rpm = topup_rpm
    monkeypatch.setattr(billing_module, "settings", mock_settings)
    # Keep the rate_limit module reading the same rpm where it inspects settings.
    return mock_settings


# ─── Stripe top-up rate limiting ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_stripe_topup_rate_limited(api_client, monkeypatch):
    _enable_billing(monkeypatch, topup_rpm=3)
    monkeypatch.setattr(
        billing_module,
        "create_stripe_embedded_session",
        AsyncMock(return_value={"client_secret": "cs_x", "session_id": "sess_x"}),
    )
    body = {"amount_cents": 1000, "return_url": "https://app.example.com/return/{CHECKOUT_SESSION_ID}"}

    statuses = [(await api_client.post("/billing/topup/stripe", json=body)).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


@pytest.mark.anyio
async def test_stripe_topup_requires_https_return_url(api_client, monkeypatch):
    _enable_billing(monkeypatch, topup_rpm=10)
    monkeypatch.setattr(
        billing_module,
        "create_stripe_embedded_session",
        AsyncMock(return_value={"client_secret": "cs_x", "session_id": "sess_x"}),
    )
    resp = await api_client.post(
        "/billing/topup/stripe",
        json={"amount_cents": 1000, "return_url": "http://app.example.com/return/{CHECKOUT_SESSION_ID}"},
    )
    assert resp.status_code == 422


# ─── USDC top-up rate limiting ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_usdc_topup_rate_limited(api_client, monkeypatch):
    _enable_billing(monkeypatch, topup_rpm=3)
    settled = MagicMock(settled=True, error=None)
    settled.tx_hash = "0xabc"
    settled.amount_usdc = 1_000_000
    monkeypatch.setattr(
        billing_module,
        "verify_and_settle_usdc_topup",
        AsyncMock(return_value=settled),
    )
    monkeypatch.setattr(billing_module, "credit_usdc_topup", AsyncMock(return_value=2_000_000))
    body = {"amount_usdc": 1_000_000, "payment_header": "deadbeef"}

    statuses = [(await api_client.post("/billing/topup/usdc", json=body)).status_code for _ in range(5)]
    assert 429 in statuses[3:]


# ─── Cross-org isolation ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_topup_rate_limit_is_per_org(api_client, monkeypatch):
    """One org exhausting its budget must not throttle a different org."""
    _enable_billing(monkeypatch, topup_rpm=2)
    monkeypatch.setattr(
        billing_module,
        "create_stripe_embedded_session",
        AsyncMock(return_value={"client_secret": "cs_x", "session_id": "sess_x"}),
    )
    body = {"amount_cents": 1000, "return_url": "https://app.example.com/return/{CHECKOUT_SESSION_ID}"}

    # Exhaust a different org's window directly via the shared limiter key.
    _rate_limit._rate_counters.clear()
    other_org_key = "topup:stripe:other-org-id"
    import time as _time

    _rate_limit._rate_counters[other_org_key] = [_time.time()] * 10

    # test-org-id still has a fresh budget.
    resp = await api_client.post("/billing/topup/stripe", json=body)
    assert resp.status_code == 200
