"""API tests for billing endpoints.

Covers:
  GET /billing/pricing         — public, no auth required
  GET /billing/history         — auth required, scoped to authenticated user
  GET /admin/billing/revenue   — admin only
  GET /billing/balance         — auth required, org-scoped
  GET /billing/invoices        — auth required, cursor paginated
  GET /billing/invoice/{run_id}— auth required, 404 on miss
  POST /admin/credits/topup    — admin only
  GET /billing/credit-history  — auth required, org-scoped

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
    monkeypatch.setattr("app.get_revenue_summary", AsyncMock(return_value=mock_summary))

    resp = await admin_api_client.get("/admin/billing/revenue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_settlements"] == 5
    assert data["total_revenue_usdc"] == 50_000


@pytest.mark.anyio
async def test_admin_billing_revenue_with_date_range(admin_api_client, monkeypatch):
    mock_fn = AsyncMock(return_value={"total_settlements": 2, "total_revenue_usdc": 20_000})
    monkeypatch.setattr("app.get_revenue_summary", mock_fn)

    resp = await admin_api_client.get("/admin/billing/revenue?start=2026-01-01&end=2026-12-31")
    assert resp.status_code == 200
    # Verify the function was actually called with parsed datetime args
    mock_fn.assert_called_once()
    start_arg, end_arg = mock_fn.call_args.args
    assert start_arg is not None
    assert end_arg is not None


# ─── /billing/balance ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_billing_balance_returns_balance(api_client, monkeypatch):
    monkeypatch.setattr("app.get_credit_balance", AsyncMock(return_value=500_000))

    resp = await api_client.get("/billing/balance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance_usdc"] == 500_000
    assert data["org_id"] == "test-org-id"


@pytest.mark.anyio
async def test_billing_balance_requires_auth(anon_client):
    resp = await anon_client.get("/billing/balance")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_billing_balance_no_org_id_returns_400(anon_client, test_settings, monkeypatch):
    """A token with empty org_id (config-based credential) should return 400."""
    from app import app
    from auth import require_auth

    async def _mock_no_org():
        return {
            "sub": "client-id",
            "org_id": "",
            "role": "user",
            "auth_method": "client_credentials",
        }

    app.dependency_overrides[require_auth] = _mock_no_org
    try:
        async with __import__("httpx").AsyncClient(
            transport=__import__("httpx").ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/billing/balance")
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.pop(require_auth, None)


# ─── /billing/invoices ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_billing_invoices_returns_items(api_client, monkeypatch):
    mock_invoices = [
        {
            "id": "evt-1",
            "run_id": "run-1",
            "thread_id": "t-1",
            "tokens_in": 100,
            "tokens_out": 50,
            "tool_calls": 0,
            "tool_names": [],
            "duration_ms": 200,
            "cost_usdc": 10_000,
            "settlement_tx": "",
            "settlement_status": "none",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    ]
    monkeypatch.setattr("app.get_invoices", AsyncMock(return_value=mock_invoices))

    resp = await api_client.get("/billing/invoices")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["run_id"] == "run-1"
    assert "2026-01-01" in data["items"][0]["created_at"]
    assert data["next_cursor"] is not None


@pytest.mark.anyio
async def test_billing_invoices_empty_returns_null_cursor(api_client, monkeypatch):
    monkeypatch.setattr("app.get_invoices", AsyncMock(return_value=[]))

    resp = await api_client.get("/billing/invoices")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


@pytest.mark.anyio
async def test_billing_invoices_passes_cursor(api_client, monkeypatch):
    mock_fn = AsyncMock(return_value=[])
    monkeypatch.setattr("app.get_invoices", mock_fn)

    await api_client.get("/billing/invoices?cursor=2026-01-01T00:00:00")

    mock_fn.assert_called_once()
    cursor_arg = mock_fn.call_args.args[2]
    assert cursor_arg is not None


@pytest.mark.anyio
async def test_billing_invoices_requires_auth(anon_client):
    resp = await anon_client.get("/billing/invoices")
    assert resp.status_code == 401


# ─── /billing/invoice/{run_id} ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_billing_invoice_by_run_found(api_client, monkeypatch):
    mock_invoice = {
        "id": "evt-1",
        "run_id": "run-abc",
        "thread_id": "t-1",
        "tokens_in": 100,
        "tokens_out": 50,
        "tool_calls": 0,
        "tool_names": [],
        "duration_ms": 200,
        "cost_usdc": 10_000,
        "settlement_tx": "",
        "settlement_status": "none",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    monkeypatch.setattr("app.get_invoice_by_run", AsyncMock(return_value=mock_invoice))

    resp = await api_client.get("/billing/invoice/run-abc")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == "run-abc"
    assert "2026-01-01" in data["created_at"]


@pytest.mark.anyio
async def test_billing_invoice_by_run_not_found(api_client, monkeypatch):
    monkeypatch.setattr("app.get_invoice_by_run", AsyncMock(return_value=None))

    resp = await api_client.get("/billing/invoice/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_billing_invoice_by_run_requires_auth(anon_client):
    resp = await anon_client.get("/billing/invoice/run-abc")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_billing_invoice_scoped_to_authenticated_user(api_client, monkeypatch):
    """The endpoint must scope the lookup to the authenticated user's sub."""
    mock_fn = AsyncMock(return_value=None)
    monkeypatch.setattr("app.get_invoice_by_run", mock_fn)

    await api_client.get("/billing/invoice/run-xyz")

    mock_fn.assert_called_once_with("run-xyz", "test-user-id")


# ─── /admin/credits/topup ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_credits_topup_success(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.admin_topup_credit", AsyncMock(return_value=1_500_000))

    resp = await admin_api_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": 1_000_000},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["org_id"] == "org-abc"
    assert data["new_balance_usdc"] == 1_500_000


@pytest.mark.anyio
async def test_admin_credits_topup_requires_admin(api_client):
    resp = await api_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": 1_000_000},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_admin_credits_topup_requires_auth(anon_client):
    resp = await anon_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": 1_000_000},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_admin_credits_topup_rejects_zero_amount(admin_api_client):
    resp = await admin_api_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": 0},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_admin_credits_topup_rejects_negative_amount(admin_api_client):
    resp = await admin_api_client.post(
        "/admin/credits/topup",
        json={"org_id": "org-abc", "amount_usdc": -500},
    )
    assert resp.status_code == 422


# ─── /billing/credit-history ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_billing_credit_history_returns_items(api_client, monkeypatch):
    mock_entries = [
        {
            "id": "ledger-1",
            "org_id": "test-org-id",
            "operation": "topup",
            "amount_usdc": 1_000_000,
            "balance_usdc_after": 1_000_000,
            "reason": "manual topup",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    ]
    monkeypatch.setattr("app.get_credit_history", AsyncMock(return_value=mock_entries))

    resp = await api_client.get("/billing/credit-history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["operation"] == "topup"
    assert "2026-01-01" in data["items"][0]["created_at"]
    assert data["next_cursor"] is not None


@pytest.mark.anyio
async def test_billing_credit_history_empty(api_client, monkeypatch):
    monkeypatch.setattr("app.get_credit_history", AsyncMock(return_value=[]))

    resp = await api_client.get("/billing/credit-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


@pytest.mark.anyio
async def test_billing_credit_history_operation_filter(api_client, monkeypatch):
    mock_fn = AsyncMock(return_value=[])
    monkeypatch.setattr("app.get_credit_history", mock_fn)

    await api_client.get("/billing/credit-history?operation=debit")

    mock_fn.assert_called_once()
    assert mock_fn.call_args.args[1] == "debit"


@pytest.mark.anyio
async def test_billing_credit_history_invalid_operation(api_client, monkeypatch):
    monkeypatch.setattr("app.get_credit_history", AsyncMock(return_value=[]))

    resp = await api_client.get("/billing/credit-history?operation=unknown")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_billing_credit_history_requires_auth(anon_client):
    resp = await anon_client.get("/billing/credit-history")
    assert resp.status_code == 401


# ─── GET /billing/topup/usdc/requirements ────────────────────────────────────


@pytest.mark.anyio
async def test_usdc_requirements_returns_accepts_array(api_client, monkeypatch):
    """Returns 200 with an accepts array containing at least one payment requirement."""
    mock_req = MagicMock()
    mock_req.model_dump.return_value = {
        "scheme": "exact",
        "network": "base-sepolia",
        "max_amount_required": 1_000_000,
        "pay_to": "0xTREASURY",
    }
    monkeypatch.setattr("app.build_usdc_topup_requirements", MagicMock(return_value=[mock_req]))

    resp = await api_client.get("/billing/topup/usdc/requirements?amount_usdc=1000000")
    assert resp.status_code == 200
    data = resp.json()
    assert "accepts" in data
    assert isinstance(data["accepts"], list)
    assert len(data["accepts"]) == 1
    assert data["accepts"][0]["scheme"] == "exact"
    assert data["x402Version"] == 2


@pytest.mark.anyio
async def test_usdc_requirements_billing_disabled_returns_503(api_client, monkeypatch):
    """Returns 503 when billing is not initialised (RuntimeError from _get_server)."""
    monkeypatch.setattr(
        "app.build_usdc_topup_requirements",
        MagicMock(side_effect=RuntimeError("Billing not initialised")),
    )

    resp = await api_client.get("/billing/topup/usdc/requirements?amount_usdc=1000000")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_usdc_requirements_below_min_returns_422(api_client):
    """amount_usdc below 1_000_000 fails Pydantic validation → 422."""
    resp = await api_client.get("/billing/topup/usdc/requirements?amount_usdc=999999")
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_usdc_requirements_requires_auth(anon_client):
    resp = await anon_client.get("/billing/topup/usdc/requirements?amount_usdc=1000000")
    assert resp.status_code == 401


# ─── POST /billing/topup/usdc ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_usdc_topup_success_credits_org(api_client, monkeypatch):
    """Valid payment header → 200 with status 'credited' and new balance."""
    from billing import BillingResult

    mock_result = BillingResult(settled=True, tx_hash="0xABC123", amount_usdc=1_000_000)
    monkeypatch.setattr("app.verify_and_settle_usdc_topup", AsyncMock(return_value=mock_result))
    monkeypatch.setattr("app.credit_usdc_topup", AsyncMock(return_value=2_000_000))

    resp = await api_client.post(
        "/billing/topup/usdc",
        json={"amount_usdc": 1_000_000, "payment_header": "dGVzdA=="},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "credited"
    assert data["amount_usdc"] == 1_000_000
    assert data["balance_usdc"] == 2_000_000
    assert data["tx_hash"] == "0xABC123"


@pytest.mark.anyio
async def test_usdc_topup_duplicate_tx_returns_409(api_client, monkeypatch):
    """Duplicate tx_hash (already processed) → 409 Conflict."""
    from billing import BillingResult

    mock_result = BillingResult(settled=True, tx_hash="0xDUPE", amount_usdc=1_000_000)
    monkeypatch.setattr("app.verify_and_settle_usdc_topup", AsyncMock(return_value=mock_result))
    monkeypatch.setattr("app.credit_usdc_topup", AsyncMock(return_value=None))

    resp = await api_client.post(
        "/billing/topup/usdc",
        json={"amount_usdc": 1_000_000, "payment_header": "dGVzdA=="},
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_usdc_topup_failed_verification_returns_402(api_client, monkeypatch):
    """Failed verify/settle → 402 Payment Required."""
    from billing import BillingResult

    mock_result = BillingResult(settled=False, error="Invalid signature")
    monkeypatch.setattr("app.verify_and_settle_usdc_topup", AsyncMock(return_value=mock_result))

    resp = await api_client.post(
        "/billing/topup/usdc",
        json={"amount_usdc": 1_000_000, "payment_header": "dGVzdA=="},
    )
    assert resp.status_code == 402


@pytest.mark.anyio
async def test_usdc_topup_billing_disabled_returns_503(api_client, monkeypatch):
    """RuntimeError from verify_and_settle → 503 when billing is disabled."""
    monkeypatch.setattr(
        "app.verify_and_settle_usdc_topup",
        AsyncMock(side_effect=RuntimeError("Billing not initialised")),
    )

    resp = await api_client.post(
        "/billing/topup/usdc",
        json={"amount_usdc": 1_000_000, "payment_header": "dGVzdA=="},
    )
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_usdc_topup_amount_below_min_returns_422(api_client):
    """amount_usdc below 1_000_000 fails Pydantic validation → 422."""
    resp = await api_client.post(
        "/billing/topup/usdc",
        json={"amount_usdc": 999_999, "payment_header": "dGVzdA=="},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_usdc_topup_requires_auth(anon_client):
    resp = await anon_client.post(
        "/billing/topup/usdc",
        json={"amount_usdc": 1_000_000, "payment_header": "dGVzdA=="},
    )
    assert resp.status_code == 401


# ─── /admin/pricing/tools & /billing/pricing tool_overrides ──────────────────


@pytest.mark.anyio
async def test_billing_pricing_includes_tool_overrides(api_client, monkeypatch):
    """GET /billing/pricing response includes tool_overrides dict."""
    import app as app_module

    mock_rule = PricingRule(
        id="usage-based-v1",
        name="Usage-based v1",
        run_price_usdc=10_000,
        tokens_in_cost_per_1k=1500,
        tokens_out_cost_per_1k=7500,
        tool_call_cost=1000,
    )
    mock_settings = MagicMock(wraps=app_module.settings)
    mock_settings.billing_enabled = True
    mock_settings.x402_network = "eip155:8453"
    monkeypatch.setattr(app_module, "settings", mock_settings)
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=mock_rule))
    monkeypatch.setattr(
        "app.get_tool_pricing_overrides",
        AsyncMock(return_value={"web_search": 15000, "get_token_price": 2000}),
    )

    resp = await api_client.get("/billing/pricing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["billing_enabled"] is True
    assert "tool_overrides" in data["pricing"]
    assert data["pricing"]["tool_overrides"]["web_search"] == 15000
    assert data["pricing"]["tool_overrides"]["get_token_price"] == 2000


@pytest.mark.anyio
async def test_admin_upsert_tool_pricing_success(admin_api_client, monkeypatch):
    """POST /admin/pricing/tools with a known tool name succeeds."""
    monkeypatch.setattr("app.upsert_tool_pricing_override", AsyncMock(return_value=None))

    resp = await admin_api_client.post(
        "/admin/pricing/tools",
        json={"tool_name": "web_search", "cost_usdc": 15000, "description": "Tavily override"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tool_name"] == "web_search"
    assert data["cost_usdc"] == 15000
    assert data["updated"] is True


@pytest.mark.anyio
async def test_admin_upsert_tool_pricing_requires_admin(api_client, monkeypatch):
    """Non-admin JWT → 403 for POST /admin/pricing/tools."""
    resp = await api_client.post(
        "/admin/pricing/tools",
        json={"tool_name": "web_search", "cost_usdc": 15000, "description": ""},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_admin_upsert_unknown_tool_rejected(admin_api_client, monkeypatch):
    """tool_name not present in the tool registry → 400."""
    resp = await admin_api_client.post(
        "/admin/pricing/tools",
        json={"tool_name": "nonexistent_tool_xyz", "cost_usdc": 9999, "description": ""},
    )
    assert resp.status_code == 400
    assert "Unknown tool name" in resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_upsert_cost_above_max_rejected(admin_api_client):
    """cost_usdc > 100_000_000 → 422 Pydantic validation error."""
    resp = await admin_api_client.post(
        "/admin/pricing/tools",
        json={"tool_name": "web_search", "cost_usdc": 200_000_000, "description": ""},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_admin_delete_tool_pricing_success(admin_api_client, monkeypatch):
    """DELETE /admin/pricing/tools/{tool_name} returns {deleted: true}."""
    monkeypatch.setattr("app.delete_tool_pricing_override", AsyncMock(return_value=True))

    resp = await admin_api_client.delete("/admin/pricing/tools/web_search")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] is True
    assert data["tool_name"] == "web_search"


@pytest.mark.anyio
async def test_admin_delete_nonexistent_tool_pricing_returns_404(admin_api_client, monkeypatch):
    """DELETE for a tool with no override → 404."""
    monkeypatch.setattr("app.delete_tool_pricing_override", AsyncMock(return_value=False))

    resp = await admin_api_client.delete("/admin/pricing/tools/nonexistent_tool")
    assert resp.status_code == 404
    assert "No pricing override found" in resp.json()["detail"]

