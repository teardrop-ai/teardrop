"""API-layer tests for POST /billing/topup/webhook.

Tests verify HTTP semantics of the endpoint: correct status codes for
each outcome (success, bad signature, bad payload, DB error, oversized body).
`handle_stripe_webhook` is mocked entirely so no DB or Stripe credentials are
needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import stripe as _stripe


@pytest.mark.anyio
async def test_valid_webhook_returns_200(anon_client, monkeypatch):
    """A well-formed, signed event is processed and returns 200 OK."""
    monkeypatch.setattr("app.handle_stripe_webhook", AsyncMock(return_value=None))
    resp = await anon_client.post(
        "/billing/topup/webhook",
        content=b'{"type":"checkout.session.completed"}',
        headers={"stripe-signature": "t=1,v1=abc"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_invalid_signature_returns_400(anon_client, monkeypatch):
    """Bad Stripe signature → 400 Bad Request."""
    monkeypatch.setattr(
        "app.handle_stripe_webhook",
        AsyncMock(side_effect=_stripe.SignatureVerificationError("bad sig", "t=1,v1=bad")),
    )
    resp = await anon_client.post(
        "/billing/topup/webhook",
        content=b'{"type":"checkout.session.completed"}',
        headers={"stripe-signature": "t=1,v1=bad"},
    )
    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_missing_sig_header_returns_400(anon_client, monkeypatch):
    """Missing Stripe-Signature header raises ValueError → 400 Bad Request."""
    monkeypatch.setattr(
        "app.handle_stripe_webhook",
        AsyncMock(side_effect=ValueError("Missing Stripe-Signature header")),
    )
    resp = await anon_client.post(
        "/billing/topup/webhook",
        content=b'{"type":"checkout.session.completed"}',
    )
    assert resp.status_code == 400
    assert "payload" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_payload_too_large_returns_400(anon_client, monkeypatch):
    """Payload exceeding 1 MB is rejected before calling handle_stripe_webhook."""
    mock_handler = AsyncMock(return_value=None)
    monkeypatch.setattr("app.handle_stripe_webhook", mock_handler)
    oversized = b"x" * (1 * 1024 * 1024 + 1)
    resp = await anon_client.post(
        "/billing/topup/webhook",
        content=oversized,
        headers={"stripe-signature": "t=1,v1=abc"},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()
    mock_handler.assert_not_called()


@pytest.mark.anyio
async def test_db_error_returns_500(test_settings, monkeypatch):
    """Unhandled DB exception becomes a 500 response so Stripe retries the event.

    Uses raise_app_exceptions=False so the HTTP response is inspectable rather
    than the exception propagating through the test transport.
    """
    from app import app
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setattr(
        "app.handle_stripe_webhook",
        AsyncMock(side_effect=RuntimeError("DB connection lost")),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/billing/topup/webhook",
            content=b'{"type":"checkout.session.completed"}',
            headers={"stripe-signature": "t=1,v1=abc"},
        )
    assert resp.status_code == 500


@pytest.mark.anyio
async def test_rate_limited_returns_429(anon_client, monkeypatch):
    """Exceeding the per-IP rate limit returns 429 before the handler is called."""
    from unittest.mock import AsyncMock as _AsyncMock

    mock_handler = _AsyncMock(return_value=None)
    monkeypatch.setattr("app.handle_stripe_webhook", mock_handler)
    # Simulate rate limit exhausted: allowed=False
    monkeypatch.setattr(
        "app._check_rate_limit",
        _AsyncMock(return_value=(False, 0, None)),
    )
    resp = await anon_client.post(
        "/billing/topup/webhook",
        content=b'{"type":"checkout.session.completed"}',
        headers={"stripe-signature": "t=1,v1=abc"},
    )
    assert resp.status_code == 429
    mock_handler.assert_not_called()
