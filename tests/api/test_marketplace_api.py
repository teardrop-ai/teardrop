"""API tests for MCP marketplace endpoints and JSON-RPC handler."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from billing import BillingResult
from marketplace import AuthorConfig, AuthorEarning, AuthorEarningByTool, AuthorWithdrawal, MarketplaceTool

_NOW = datetime.now(timezone.utc)

_VALID_ADDR = "0x1234567890123456789012345678901234567890"

_AUTHOR_CONFIG = AuthorConfig(
    org_id="test-org-id",
    settlement_wallet=_VALID_ADDR,
    created_at=_NOW,
    updated_at=_NOW,
)


# ─── POST /marketplace/author-config ─────────────────────────────────────────


@pytest.mark.anyio
async def test_set_author_config_success(admin_api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.marketplace.set_author_config", AsyncMock(return_value=_AUTHOR_CONFIG))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await admin_api_client.post(
        "/marketplace/author-config",
        json={
            "settlement_wallet": _VALID_ADDR,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["settlement_wallet"] == _VALID_ADDR

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_set_author_config_forbidden_for_member(api_client, monkeypatch):
    """Non-admin members cannot change the settlement wallet (financial control)."""
    set_mock = AsyncMock(return_value=_AUTHOR_CONFIG)
    monkeypatch.setattr("teardrop.routers.marketplace.set_author_config", set_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/marketplace/author-config",
        json={"settlement_wallet": _VALID_ADDR},
    )
    assert resp.status_code == 403
    set_mock.assert_not_awaited()

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_set_author_config_invalid_wallet(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.marketplace.set_author_config",
        AsyncMock(side_effect=ValueError("Invalid wallet address")),
    )
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await admin_api_client.post(
        "/marketplace/author-config",
        json={
            "settlement_wallet": "0x" + "00" * 20,  # zero address
        },
    )
    assert resp.status_code == 422

    config.get_settings.cache_clear()


# ─── GET /marketplace/author-config ──────────────────────────────────────────


@pytest.mark.anyio
async def test_get_author_config_success(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.marketplace.get_author_config", AsyncMock(return_value=_AUTHOR_CONFIG))

    resp = await api_client.get("/marketplace/author-config")
    assert resp.status_code == 200
    assert resp.json()["settlement_wallet"] == _AUTHOR_CONFIG.settlement_wallet


@pytest.mark.anyio
async def test_get_author_config_not_configured(api_client, monkeypatch):
    """When no config exists the endpoint returns 200 with null fields so the
    dashboard can render the 'not yet configured' state instead of treating it
    as an error."""
    monkeypatch.setattr("teardrop.routers.marketplace.get_author_config", AsyncMock(return_value=None))

    resp = await api_client.get("/marketplace/author-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["settlement_wallet"] is None


# ─── GET /marketplace/balance ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_balance(api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.marketplace.get_author_balance", AsyncMock(return_value=50_000))

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
        "teardrop.routers.marketplace.get_author_earnings_history",
        AsyncMock(return_value=([earning], None)),
    )

    resp = await api_client.get("/marketplace/earnings")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["earnings"]) == 1
    assert data["earnings"][0]["author_share_usdc"] == 7_000


@pytest.mark.anyio
async def test_get_earnings_by_tool(api_client, monkeypatch):
    summary = AuthorEarningByTool(
        tool_name="my_tool",
        total_calls=3,
        total_amount_usdc=30_000,
        total_author_share_usdc=21_000,
        pending_author_share_usdc=14_000,
        settled_author_share_usdc=7_000,
        total_platform_share_usdc=9_000,
    )
    monkeypatch.setattr(
        "teardrop.routers.marketplace.get_author_earnings_by_tool",
        AsyncMock(return_value=[summary]),
    )
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.get("/marketplace/earnings/by-tool")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tools"] == [
        {
            "tool_name": "my_tool",
            "total_calls": 3,
            "total_amount_usdc": 30_000,
            "total_author_share_usdc": 21_000,
            "pending_author_share_usdc": 14_000,
            "settled_author_share_usdc": 7_000,
            "total_platform_share_usdc": 9_000,
        }
    ]

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_get_earnings_by_tool_marketplace_disabled(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.get("/marketplace/earnings/by-tool")
    assert resp.status_code == 404

    config.get_settings.cache_clear()


# ─── POST /marketplace/withdraw ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_request_withdrawal_success(admin_api_client, monkeypatch):
    withdrawal = AuthorWithdrawal(
        id="w-1",
        org_id="test-org-id",
        amount_usdc=200_000,
        tx_hash="",
        wallet=_VALID_ADDR,
        status="pending",
        created_at=_NOW,
    )
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", AsyncMock(return_value=withdrawal))

    resp = await admin_api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


@pytest.mark.anyio
async def test_request_withdrawal_forbidden_for_member(api_client, monkeypatch):
    """Non-admin members cannot move funds out of the org balance."""
    withdraw_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", withdraw_mock)

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 403
    withdraw_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_request_withdrawal_insufficient(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.marketplace.request_withdrawal",
        AsyncMock(side_effect=ValueError("Insufficient balance")),
    )

    resp = await admin_api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
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
        category="defi",
        total_calls=42,
    )
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog", AsyncMock(return_value=[tool]))
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tools"]) == 1
    assert data["tools"][0]["name"] == "acme/my_tool"
    assert data["tools"][0]["qualified_name"] == "acme/my_tool"
    assert data["tools"][0]["tool_name"] == "my_tool"
    assert data["tools"][0]["tool_type"] == "community"
    assert data["tools"][0]["category"] == "defi"
    assert data["tools"][0]["total_calls"] == 42
    assert data["tools"][0]["health_status"] == "healthy"
    assert data["tools"][0]["is_healthy"] is True
    assert resp.headers["cache-control"] == "public, max-age=60"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_catalog_category_and_popularity_params(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="price_feed",
        qualified_name="acme/price_feed",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1000,
        author_org_name="Acme",
        author_org_slug="acme",
        category="defi",
        total_calls=12,
    )
    catalog_mock = AsyncMock(return_value=[tool])
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog", catalog_mock)
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog?category=defi&sort=popularity")

    assert resp.status_code == 200
    catalog_mock.assert_awaited_once()
    assert catalog_mock.call_args.kwargs["category"] == "defi"
    assert catalog_mock.call_args.kwargs["sort"] == "popularity"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_catalog_rejects_invalid_category(anon_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog?category=bad")

    assert resp.status_code == 400
    assert "Invalid category" in resp.json()["detail"]

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_catalog_detail_success(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="my_tool",
        qualified_name="acme/my_tool",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1000,
        author_org_name="Acme",
        author_org_slug="acme",
        category="utility",
        total_calls=7,
    )
    detail_mock = AsyncMock(return_value=tool)
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog_tool", detail_mock)
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog/acme/my_tool")

    assert resp.status_code == 200
    assert resp.json()["tool"]["name"] == "acme/my_tool"
    assert resp.json()["tool"]["total_calls"] == 7
    detail_mock.assert_awaited_once()

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_author_profile_success(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="my_tool",
        qualified_name="acme/my_tool",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1000,
        author_org_name="Acme",
        author_org_slug="acme",
        total_calls=7,
    )
    monkeypatch.setattr(
        "teardrop.routers.marketplace.get_marketplace_author_summary",
        AsyncMock(return_value={"org_slug": "acme", "org_name": "Acme", "tool_count": 1, "total_calls": 7}),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog", AsyncMock(return_value=[tool]))
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/authors/acme")

    assert resp.status_code == 200
    data = resp.json()
    assert data["org_slug"] == "acme"
    assert data["total_calls"] == 7
    assert data["tools"][0]["name"] == "acme/my_tool"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_marketplace_llms_txt(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="my_tool",
        qualified_name="acme/my_tool",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1234567,
        author_org_name="Acme",
        author_org_slug="acme",
        category="utility",
        total_calls=7,
    )
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog", AsyncMock(return_value=[tool]))
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/llms.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "acme/my_tool" in resp.text
    assert "$1.234567" in resp.text

    config.get_settings.cache_clear()


# ─── POST /mcp/v1 (JSON-RPC) ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mcp_initialize(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["serverInfo"]["name"] == "teardrop-marketplace"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_tools_list(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

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
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_marketplace_catalog", AsyncMock(return_value=[tool]))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.registry.list_latest", MagicMock(return_value=[]))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["result"]["tools"]) == 1
    assert data["result"]["tools"][0]["name"] == "acme/my_tool"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_tools_call_not_found(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_marketplace_tool_by_name", AsyncMock(return_value=None))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "acme/nonexistent", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_invalid_jsonrpc(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "1.0",
            "id": 1,
            "method": "initialize",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32600

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_unknown_method(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "fake/method",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32601

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_rate_limited(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(False, 0, 0)))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
        },
    )
    assert resp.status_code == 429

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_disabled(api_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
        },
    )
    assert resp.status_code == 404

    config.get_settings.cache_clear()


# ─── POST /mcp/v1 — tools/call ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_mcp_tools_call_success(api_client, monkeypatch):
    """Marketplace tool call executes webhook and returns isError:false."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.get_marketplace_tool_by_name",
        AsyncMock(return_value={"org_id": "author-org-id", "name": "my_tool"}),
    )
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp._execute_marketplace_tool",
        AsyncMock(return_value={"answer": 42}),
    )

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "acme/my_tool", "arguments": {}},
        },
    )
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
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.verify_credit",
        AsyncMock(return_value=BillingResult(verified=False, error="Insufficient balance")),
    )

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "acme/my_tool", "arguments": {}},
        },
    )
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
    monkeypatch.setattr("teardrop.routers.admin.process_withdrawal", AsyncMock(return_value=withdrawal))

    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/w-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "w-1"
    assert data["status"] == "settled"


@pytest.mark.anyio
async def test_admin_process_withdrawal_not_found(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.admin.process_withdrawal",
        AsyncMock(side_effect=ValueError("Withdrawal not found or not in 'pending' status")),
    )

    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/bad-id")
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_admin_complete_withdrawal_success(admin_api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.admin.complete_withdrawal", AsyncMock(return_value=None))

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
    monkeypatch.setattr("teardrop.routers.admin.list_pending_withdrawals", AsyncMock(return_value=[withdrawal]))

    resp = await admin_api_client.get("/admin/marketplace/withdrawals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["withdrawals"]) == 1
    assert data["withdrawals"][0]["id"] == "w-1"


@pytest.mark.anyio
async def test_admin_list_withdrawals_org_filter(admin_api_client, monkeypatch):
    mock_list = AsyncMock(return_value=[])
    monkeypatch.setattr("teardrop.routers.admin.list_pending_withdrawals", mock_list)

    resp = await admin_api_client.get("/admin/marketplace/withdrawals?org_id=some-org")
    assert resp.status_code == 200
    mock_list.assert_called_once_with("some-org")


# ─── Marketplace subscriptions ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_subscribe_success(api_client, monkeypatch):
    from marketplace import MarketplaceSubscription

    sub = MarketplaceSubscription(
        id="sub-1",
        org_id="test-org-id",
        qualified_tool_name="acme/weather",
        is_active=True,
        subscribed_at=_NOW,
    )
    monkeypatch.setattr("marketplace.subscribe_to_tool", AsyncMock(return_value=sub))

    resp = await api_client.post(
        "/marketplace/subscriptions",
        json={
            "qualified_tool_name": "acme/weather",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["qualified_tool_name"] == "acme/weather"
    assert data["is_active"] is True


@pytest.mark.anyio
async def test_subscribe_invalid_name(api_client, monkeypatch):
    """Reject tool names without org slug."""
    resp = await api_client.post(
        "/marketplace/subscriptions",
        json={
            "qualified_tool_name": "no_slash_here",
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_subscribe_tool_not_found(api_client, monkeypatch):
    monkeypatch.setattr(
        "marketplace.subscribe_to_tool",
        AsyncMock(side_effect=ValueError("Marketplace tool not found: bad/tool")),
    )

    resp = await api_client.post(
        "/marketplace/subscriptions",
        json={
            "qualified_tool_name": "bad/tool",
        },
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_subscribe_platform_tool_rejected(api_client, monkeypatch):
    from marketplace import PlatformToolSubscriptionError

    monkeypatch.setattr(
        "marketplace.subscribe_to_tool",
        AsyncMock(
            side_effect=PlatformToolSubscriptionError(
                "'platform/web_search' is a built-in platform tool and is always available without subscription."
            )
        ),
    )

    resp = await api_client.post(
        "/marketplace/subscriptions",
        json={
            "qualified_tool_name": "platform/web_search",
        },
    )
    assert resp.status_code == 400
    assert "always available without subscription" in resp.json()["detail"]


@pytest.mark.anyio
async def test_list_subscriptions(api_client, monkeypatch):
    from marketplace import MarketplaceSubscription

    subs = [
        MarketplaceSubscription(
            id="sub-1",
            org_id="test-org-id",
            qualified_tool_name="acme/weather",
            is_active=True,
            subscribed_at=_NOW,
        ),
    ]
    monkeypatch.setattr("marketplace.get_org_subscriptions", AsyncMock(return_value=subs))

    resp = await api_client.get("/marketplace/subscriptions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["subscriptions"]) == 1
    assert data["subscriptions"][0]["qualified_tool_name"] == "acme/weather"


@pytest.mark.anyio
async def test_unsubscribe_success(api_client, monkeypatch):
    monkeypatch.setattr("marketplace.unsubscribe_from_tool", AsyncMock(return_value=True))

    resp = await api_client.delete("/marketplace/subscriptions/sub-1")
    assert resp.status_code == 200
    assert resp.json()["unsubscribed"] is True


@pytest.mark.anyio
async def test_unsubscribe_not_found(api_client, monkeypatch):
    monkeypatch.setattr("marketplace.unsubscribe_from_tool", AsyncMock(return_value=False))

    resp = await api_client.delete("/marketplace/subscriptions/sub-99")
    assert resp.status_code == 404


# ─── Subscription enforcement ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mcp_v1_unsubscribed_tool_blocked(api_client, monkeypatch):
    """Calling a marketplace tool without a subscription returns -32001."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=False))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "acme/my_tool", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == -32001
    assert "acme/my_tool" in data["error"]["message"]

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_v1_subscribed_tool_proceeds(api_client, monkeypatch):
    """Subscribed org bypasses the subscription gate and reaches tool execution."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.get_marketplace_tool_by_name",
        AsyncMock(return_value={"org_id": "author-org", "name": "my_tool", "base_price_usdc": 0}),
    )
    monkeypatch.setattr("teardrop.routers.marketplace_mcp._execute_marketplace_tool", AsyncMock(return_value={"ok": True}))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "acme/my_tool", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    assert "result" in resp.json()

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_mcp_v1_builtin_tool_skips_subscription_check(api_client, monkeypatch):
    """Built-in tools (no '/') never hit the subscription check."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    check_sub_mock = AsyncMock(return_value=False)  # would block if called
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", check_sub_mock)
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.registry.get",
        MagicMock(return_value=None),  # built-in not found → -32601
    )

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {"name": "builtin_tool", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should reach tool-not-found, NOT the subscription gate
    assert data["error"]["code"] == -32601
    check_sub_mock.assert_not_called()

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_check_org_subscription_cache_hit(monkeypatch):
    """Second call within TTL uses cache; DB is only queried once."""
    import marketplace
    from marketplace import _SUBSCRIPTION_CACHE, MarketplaceSubscription, check_org_subscription

    sub = MarketplaceSubscription(
        id="s1",
        org_id="org-cache-hit",
        qualified_tool_name="acme/tool",
        is_active=True,
        subscribed_at=_NOW,
    )
    mock_get = AsyncMock(return_value=[sub])
    monkeypatch.setattr(marketplace, "get_org_subscriptions", mock_get)
    _SUBSCRIPTION_CACHE.pop("org-cache-hit", None)

    result1 = await check_org_subscription("org-cache-hit", "acme/tool")
    result2 = await check_org_subscription("org-cache-hit", "acme/tool")

    assert result1 is True
    assert result2 is True
    mock_get.assert_called_once()  # DB hit only once; second is cache
    _SUBSCRIPTION_CACHE.pop("org-cache-hit", None)


@pytest.mark.anyio
async def test_check_org_subscription_not_subscribed(monkeypatch):
    """Returns False when org has no active subscription for the tool."""
    import marketplace
    from marketplace import _SUBSCRIPTION_CACHE, check_org_subscription

    monkeypatch.setattr(marketplace, "get_org_subscriptions", AsyncMock(return_value=[]))
    _SUBSCRIPTION_CACHE.pop("org-no-sub", None)

    result = await check_org_subscription("org-no-sub", "acme/tool")

    assert result is False
    _SUBSCRIPTION_CACHE.pop("org-no-sub", None)


@pytest.mark.anyio
async def test_check_org_subscription_cache_invalidated_on_subscribe(monkeypatch):
    """Subscribing to a tool immediately drops the org's cache entry."""
    import time

    import marketplace
    from marketplace import _SUBSCRIPTION_CACHE

    # Prime the cache with a stale empty set
    _SUBSCRIPTION_CACHE["org-inv"] = (frozenset(), time.monotonic() + 60)

    pool_mock = MagicMock()
    pool_mock.execute = AsyncMock(return_value="INSERT 1")
    monkeypatch.setattr(marketplace, "_get_pool", MagicMock(return_value=pool_mock))
    monkeypatch.setattr(
        marketplace,
        "get_marketplace_tool_by_name",
        AsyncMock(return_value={"org_id": "author", "name": "t"}),
    )

    await marketplace.subscribe_to_tool("org-inv", "acme/t")

    assert "org-inv" not in _SUBSCRIPTION_CACHE


# ─── base_price_usdc in tool CRUD ────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_tool_with_base_price(api_client, monkeypatch):
    from org_tools import OrgTool

    tool = OrgTool(
        id="t-1",
        org_id="test-org-id",
        name="my_tool",
        description="desc",
        input_schema={"type": "object"},
        webhook_url="https://example.com/hook",
        webhook_method="GET",
        has_auth=False,
        timeout_seconds=10,
        is_active=True,
        publish_as_mcp=True,
        marketplace_description="mp desc",
        base_price_usdc=5_000_000,
        created_at=_NOW,
        updated_at=_NOW,
    )
    monkeypatch.setattr("teardrop.routers.org.tools.create_org_tool", AsyncMock(return_value=tool))
    monkeypatch.setattr("teardrop.routers.org.tools.invalidate_org_tools_cache", AsyncMock())

    resp = await api_client.post(
        "/tools",
        json={
            "name": "my_tool",
            "description": "desc",
            "input_schema": {"type": "object"},
            "webhook_url": "https://example.com/hook",
            "base_price_usdc": 5_000_000,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["base_price_usdc"] == 5_000_000


# ─── /mcp/v1 pricing resolution with base_price_usdc ─────────────────────────


@pytest.mark.anyio
async def test_mcp_tools_call_uses_author_price(api_client, monkeypatch):
    """When no admin override exists, the tool's base_price_usdc is used."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.get_marketplace_tool_by_name",
        AsyncMock(
            return_value={
                "org_id": "author-org-id",
                "name": "my_tool",
                "base_price_usdc": 2_000_000,
            }
        ),
    )
    monkeypatch.setattr("teardrop.routers.marketplace_mcp._execute_marketplace_tool", AsyncMock(return_value={"ok": True}))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "acme/my_tool", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    assert "result" in resp.json()

    config.get_settings.cache_clear()


# ─── Admin pricing guard accepts qualified names ─────────────────────────────


@pytest.mark.anyio
async def test_admin_pricing_accepts_marketplace_tool(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.admin.get_marketplace_tool_by_name",
        AsyncMock(return_value={"org_id": "o1", "name": "weather"}),
    )
    monkeypatch.setattr("teardrop.routers.admin.upsert_tool_pricing_override", AsyncMock())

    resp = await admin_api_client.post(
        "/admin/pricing/tools",
        json={
            "tool_name": "acme/weather",
            "cost_usdc": 1_000_000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] is True


# ─── MCP tools/call fires author earnings recording ──────────────────────────


@pytest.mark.anyio
async def test_mcp_tools_call_records_author_earnings(api_client, monkeypatch):
    """Successful billed MCP call schedules record_tool_call_earnings."""
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.check_org_subscription", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.get_marketplace_tool_by_name",
        AsyncMock(
            return_value={
                "org_id": "author-org-id",
                "name": "my_tool",
                "base_price_usdc": 1_000,
            }
        ),
    )
    monkeypatch.setattr(
        "teardrop.routers.marketplace_mcp.verify_credit",
        AsyncMock(return_value=BillingResult(verified=True, billing_method="credit")),
    )
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.debit_credit", AsyncMock(return_value=(True, 1_000)))
    monkeypatch.setattr("teardrop.routers.marketplace_mcp._execute_marketplace_tool", AsyncMock(return_value={"ok": True}))
    mock_earnings = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace_mcp.record_tool_call_earnings", mock_earnings)

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "acme/my_tool", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["isError"] is False

    # record_tool_call_earnings is called immediately (to produce the coroutine)
    # before asyncio.create_task schedules it; the mock call is registered synchronously.
    mock_earnings.assert_called_once_with(
        author_org_id="author-org-id",
        caller_org_id="test-org-id",
        tool_name="my_tool",
        total_cost_usdc=1_000,
    )

    config.get_settings.cache_clear()
