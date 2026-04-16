"""API tests for MCP marketplace endpoints and JSON-RPC handler."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from billing import BillingResult
from marketplace import AuthorConfig, AuthorEarning, AuthorWithdrawal, MarketplaceTool

_NOW = datetime.now(timezone.utc)

_VALID_ADDR = "0x1234567890123456789012345678901234567890"

_AUTHOR_CONFIG = AuthorConfig(
    org_id="test-org-id",
    settlement_wallet=_VALID_ADDR,
    revenue_share_bps=7000,
    created_at=_NOW,
    updated_at=_NOW,
)


# ─── POST /marketplace/author-config ─────────────────────────────────────────


@pytest.mark.anyio
async def test_set_author_config_success(api_client, monkeypatch):
    monkeypatch.setattr("app.set_author_config", AsyncMock(return_value=_AUTHOR_CONFIG))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/marketplace/author-config", json={
        "settlement_wallet": _VALID_ADDR,
        "revenue_share_bps": 7000,
    })
    assert resp.status_code == 200
    assert resp.json()["revenue_share_bps"] == 7000

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_set_author_config_invalid_wallet(api_client, monkeypatch):
    monkeypatch.setattr(
        "app.set_author_config",
        AsyncMock(side_effect=ValueError("Invalid wallet address")),
    )
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/marketplace/author-config", json={
        "settlement_wallet": "0x" + "00" * 20,  # zero address
    })
    assert resp.status_code == 422

    config.get_settings.cache_clear()


# ─── GET /marketplace/author-config ──────────────────────────────────────────


@pytest.mark.anyio
async def test_get_author_config_success(api_client, monkeypatch):
    monkeypatch.setattr("app.get_author_config", AsyncMock(return_value=_AUTHOR_CONFIG))

    resp = await api_client.get("/marketplace/author-config")
    assert resp.status_code == 200
    assert resp.json()["settlement_wallet"] == _AUTHOR_CONFIG.settlement_wallet


@pytest.mark.anyio
async def test_get_author_config_not_configured(api_client, monkeypatch):
    """When no config exists the endpoint returns 200 with null fields so the
    dashboard can render the 'not yet configured' state instead of treating it
    as an error."""
    monkeypatch.setattr("app.get_author_config", AsyncMock(return_value=None))

    resp = await api_client.get("/marketplace/author-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["settlement_wallet"] is None
    assert body["revenue_share_bps"] is None


# ─── GET /marketplace/balance ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_balance(api_client, monkeypatch):
    monkeypatch.setattr("app.get_author_balance", AsyncMock(return_value=50_000))

    resp = await api_client.get("/marketplace/balance")
    assert resp.status_code == 200
    assert resp.json()["balance_usdc"] == 50_000


# ─── GET /marketplace/earnings ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_earnings(api_client, monkeypatch):
    earning = AuthorEarning(
        id="e-1",
        org_id="test-org-id",
        tool_name="my_tool",
        caller_org_id="caller-org",
        amount_usdc=10_000,
        author_share_usdc=7_000,
        platform_share_usdc=3_000,
        status="pending",
        created_at=_NOW,
    )
    monkeypatch.setattr(
        "app.get_author_earnings_history",
        AsyncMock(return_value=([earning], None)),
    )

    resp = await api_client.get("/marketplace/earnings")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["earnings"]) == 1
    assert data["earnings"][0]["author_share_usdc"] == 7_000


# ─── POST /marketplace/withdraw ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_request_withdrawal_success(api_client, monkeypatch):
    withdrawal = AuthorWithdrawal(
        id="w-1",
        org_id="test-org-id",
        amount_usdc=200_000,
        tx_hash="",
        wallet=_VALID_ADDR,
        status="pending",
        created_at=_NOW,
    )
    monkeypatch.setattr("app.request_withdrawal", AsyncMock(return_value=withdrawal))

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


@pytest.mark.anyio
async def test_request_withdrawal_insufficient(api_client, monkeypatch):
    monkeypatch.setattr(
        "app.request_withdrawal",
        AsyncMock(side_effect=ValueError("Insufficient balance")),
    )

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 422


# ─── GET /marketplace/catalog ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_catalog_success(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="my_tool",
        qualified_name="acme/my_tool",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1000,
        author_org_name="Acme",
        author_org_slug="acme",
    )
    monkeypatch.setattr("app.get_marketplace_catalog", AsyncMock(return_value=[tool]))
    monkeypatch.setattr("app.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import config
    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tools"]) == 1
    assert data["tools"][0]["name"] == "acme/my_tool"

    config.get_settings.cache_clear()


# ─── POST /mcp/v1 (JSON-RPC) ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mcp_initialize(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["serverInfo"]["name"] == "teardrop-marketplace"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_tools_list(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    tool = MarketplaceTool(
        name="my_tool",
        qualified_name="acme/my_tool",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1000,
        author_org_name="Acme",
        author_org_slug="acme",
    )
    monkeypatch.setattr("app.get_marketplace_catalog", AsyncMock(return_value=[tool]))
    monkeypatch.setattr("app.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("app.registry.list_latest", MagicMock(return_value=[]))

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["result"]["tools"]) == 1
    assert data["result"]["tools"][0]["name"] == "acme/my_tool"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_tools_call_not_found(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("app.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("app.get_marketplace_tool_by_name", AsyncMock(return_value=None))

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "acme/nonexistent", "arguments": {}},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_invalid_jsonrpc(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "1.0",
        "id": 1,
        "method": "initialize",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32600

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_unknown_method(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "fake/method",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32601

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_rate_limited(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(False, 0, 0)))

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
    })
    assert resp.status_code == 429

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_disabled(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
    })
    assert resp.status_code == 404

    config.get_settings.cache_clear()


# ─── POST /mcp/v1 — tools/call ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_mcp_tools_call_success(api_client, monkeypatch):
    """Marketplace tool call executes webhook and returns isError:false."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("app.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.get_marketplace_tool_by_name",
        AsyncMock(return_value={"org_id": "author-org-id", "name": "my_tool"}),
    )
    monkeypatch.setattr(
        "app._execute_marketplace_tool",
        AsyncMock(return_value={"answer": 42}),
    )

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "acme/my_tool", "arguments": {}},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert data["result"]["isError"] is False
    assert data["result"]["content"][0]["type"] == "text"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_tools_call_insufficient_credit(api_client, monkeypatch):
    """Billing gate rejects calls when org has insufficient credit."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setattr("app._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("app.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("app.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.verify_credit",
        AsyncMock(return_value=BillingResult(verified=False, error="Insufficient balance")),
    )

    import config
    config.get_settings.cache_clear()

    resp = await api_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "acme/my_tool", "arguments": {}},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32000

    config.get_settings.cache_clear()


# ─── Admin marketplace endpoints ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_admin_process_withdrawal_success(admin_api_client, monkeypatch):
    withdrawal = AuthorWithdrawal(
        id="w-1",
        org_id="test-org-id",
        amount_usdc=200_000,
        tx_hash="",
        wallet=_VALID_ADDR,
        status="settled",
        created_at=_NOW,
        settled_at=_NOW,
    )
    monkeypatch.setattr("app.process_withdrawal", AsyncMock(return_value=withdrawal))

    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/w-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "w-1"
    assert data["status"] == "settled"


@pytest.mark.anyio
async def test_admin_process_withdrawal_not_found(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "app.process_withdrawal",
        AsyncMock(side_effect=ValueError("Withdrawal not found or not in 'pending' status")),
    )

    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/bad-id")
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_admin_complete_withdrawal_success(admin_api_client, monkeypatch):
    monkeypatch.setattr("app.complete_withdrawal", AsyncMock(return_value=None))

    resp = await admin_api_client.post(
        "/admin/marketplace/complete-withdrawal/w-1",
        json={"tx_hash": "0xabcdef1234567890"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["tx_hash"] == "0xabcdef1234567890"


@pytest.mark.anyio
async def test_admin_list_withdrawals(admin_api_client, monkeypatch):
    withdrawal = AuthorWithdrawal(
        id="w-1",
        org_id="test-org-id",
        amount_usdc=200_000,
        tx_hash="",
        wallet=_VALID_ADDR,
        status="pending",
        created_at=_NOW,
    )
    monkeypatch.setattr("app.list_pending_withdrawals", AsyncMock(return_value=[withdrawal]))

    resp = await admin_api_client.get("/admin/marketplace/withdrawals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["withdrawals"]) == 1
    assert data["withdrawals"][0]["id"] == "w-1"


@pytest.mark.anyio
async def test_admin_list_withdrawals_org_filter(admin_api_client, monkeypatch):
    mock_list = AsyncMock(return_value=[])
    monkeypatch.setattr("app.list_pending_withdrawals", mock_list)

    resp = await admin_api_client.get("/admin/marketplace/withdrawals?org_id=some-org")
    assert resp.status_code == 200
    mock_list.assert_called_once_with("some-org")
