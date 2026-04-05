"""API tests for billing endpoints.

Covers:
  GET /billing/pricing       — public, no auth required
  GET /billing/history       — auth required, scoped to authenticated user
  GET /admin/billing/revenue — admin only

All DB/billing functions are mocked; no live Postgres required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from billing import PricingRule


# ─── /billing/pricing ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_billing_pricing_when_disabled(api_client, monkeypatch):
    """When billing is disabled the endpoint returns {billing_enabled: false}."""
    import app as app_module
    mock_settings = MagicMock(wraps=app_module.settings)
    mock_settings.billing_enabled = False
    monkeypatch.setattr(app_module, "settings", mock_settings)

    resp = await api_client.get("/billing/pricing")
    assert resp.status_code == 200
    assert resp.json()["billing_enabled"] is False


@pytest.mark.anyio
async def test_billing_pricing_when_enabled_with_rule(api_client, monkeypatch):
    """When billing is enabled and a pricing rule exists the rule is returned."""
    import app as app_module
    mock_rule = PricingRule(id="default", name="Default", run_price_usdc=10_000)
    mock_settings = MagicMock(wraps=app_module.settings)
    mock_settings.billing_enabled = True
    mock_settings.x402_network = "base-sepolia"
    monkeypatch.setattr(app_module, "settings", mock_settings)
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=mock_rule))

    resp = await api_client.get("/billing/pricing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["billing_enabled"] is True
    assert data["pricing"]["id"] == "default"
    assert data["pricing"]["run_price_usdc"] == 10_000
    assert data["network"] == "base-sepolia"


@pytest.mark.anyio
async def test_billing_pricing_enabled_no_rule(api_client, monkeypatch):
    """When billing is enabled but DB has no pricing rule, pricing is null."""
    import app as app_module
    mock_settings = MagicMock(wraps=app_module.settings)
    mock_settings.billing_enabled = True
    monkeypatch.setattr(app_module, "settings", mock_settings)
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=None))

    resp = await api_client.get("/billing/pricing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["billing_enabled"] is True
    assert data["pricing"] is None


@pytest.mark.anyio
async def test_billing_pricing_no_auth_required(anon_client, monkeypatch):
    """The pricing endpoint is public — unauthenticated requests succeed."""
    import app as app_module
    mock_settings = MagicMock(wraps=app_module.settings)
    mock_settings.billing_enabled = False
    monkeypatch.setattr(app_module, "settings", mock_settings)

    resp = await anon_client.get("/billing/pricing")
    assert resp.status_code == 200


# ─── /billing/history ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_billing_history_returns_list(api_client, monkeypatch):
    mock_history = [
        {
            "id": "evt-1",
            "run_id": "run-1",
            "tokens_in": 100,
            "tokens_out": 50,
            "tool_calls": 0,
            "duration_ms": 200,
            "cost_usdc": 10_000,
            "settlement_tx": "0xabc",
            "settlement_status": "settled",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    ]
    monkeypatch.setattr("app.get_billing_history", AsyncMock(return_value=mock_history))

    resp = await api_client.get("/billing/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["run_id"] == "run-1"
    # created_at should be serialized to ISO string
    assert "2026-01-01" in data[0]["created_at"]


@pytest.mark.anyio
async def test_billing_history_empty_list(api_client, monkeypatch):
    monkeypatch.setattr("app.get_billing_history", AsyncMock(return_value=[]))

    resp = await api_client.get("/billing/history")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_billing_history_requires_auth(anon_client):
    resp = await anon_client.get("/billing/history")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_billing_history_scoped_to_authenticated_user(api_client, monkeypatch):
    """The endpoint must call get_billing_history with the authenticated user's sub."""
    mock_fn = AsyncMock(return_value=[])
    monkeypatch.setattr("app.get_billing_history", mock_fn)

    await api_client.get("/billing/history")

    mock_fn.assert_called_once()
    # First positional arg should be the test user's sub
    assert mock_fn.call_args.args[0] == "test-user-id"


@pytest.mark.anyio
async def test_billing_history_limit_capped_at_200(api_client, monkeypatch):
    """Requesting more than 200 rows should be silently capped to 200."""
    mock_fn = AsyncMock(return_value=[])
    monkeypatch.setattr("app.get_billing_history", mock_fn)

    await api_client.get("/billing/history?limit=999")

    mock_fn.assert_called_once()
    # Second arg is min(limit, 200) = 200
    assert mock_fn.call_args.args[1] == 200


# ─── /admin/billing/revenue ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_billing_revenue_requires_admin(api_client):
    """Regular user (role=user) must receive 403."""
    resp = await api_client.get("/admin/billing/revenue")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_admin_billing_revenue_requires_auth(anon_client):
    """Unauthenticated user must receive 401."""
    resp = await anon_client.get("/admin/billing/revenue")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_admin_billing_revenue_returns_summary(admin_api_client, monkeypatch):
    mock_summary = {"total_settlements": 5, "total_revenue_usdc": 50_000}
    monkeypatch.setattr(
        "app.get_revenue_summary", AsyncMock(return_value=mock_summary)
    )

    resp = await admin_api_client.get("/admin/billing/revenue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_settlements"] == 5
    assert data["total_revenue_usdc"] == 50_000


@pytest.mark.anyio
async def test_admin_billing_revenue_with_date_range(admin_api_client, monkeypatch):
    mock_fn = AsyncMock(return_value={"total_settlements": 2, "total_revenue_usdc": 20_000})
    monkeypatch.setattr("app.get_revenue_summary", mock_fn)

    resp = await admin_api_client.get(
        "/admin/billing/revenue?start=2026-01-01&end=2026-12-31"
    )
    assert resp.status_code == 200
    # Verify the function was actually called with parsed datetime args
    mock_fn.assert_called_once()
    start_arg, end_arg = mock_fn.call_args.args
    assert start_arg is not None
    assert end_arg is not None
