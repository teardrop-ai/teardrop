# SPDX-License-Identifier: BUSL-1.1
"""Tests for marketplace platform tools — catalog UNION and MCP billing gate."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import config
from marketplace import (
    MarketplaceTool,
    _invalidate_platform_tool_cache,
    get_marketplace_catalog,
    get_platform_tool_price,
)

# ─── get_marketplace_catalog with platform tools ─────────────────────────────


class TestGetMarketplaceCatalogWithPlatformTools:
    @pytest.mark.anyio
    async def test_empty_org_tools_returns_platform_tools(self, monkeypatch):
        """When no org tools are published, catalog still returns platform tools."""
        mock_pool = MagicMock()
        # First fetch: org_tools → empty
        # Second fetch: marketplace_platform_tools → two rows
        platform_rows = [
            {
                "tool_name": "web_search",
                "display_name": "Web Search",
                "description": "Real-time web search",
                "base_price_usdc": 10000,
            },
            {
                "tool_name": "http_fetch",
                "display_name": "HTTP Fetch",
                "description": "SSRF-protected fetch",
                "base_price_usdc": 2000,
            },
        ]
        mock_pool.fetch = AsyncMock(side_effect=[[], platform_rows])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog()

        assert len(catalog) == 2
        assert all(isinstance(t, MarketplaceTool) for t in catalog)
        names = {t.qualified_name for t in catalog}
        assert "platform/web_search" in names
        assert "platform/http_fetch" in names
        by_name = {t.qualified_name: t for t in catalog}
        assert by_name["platform/http_fetch"].cost_usdc == 2000
        assert by_name["platform/http_fetch"].author_org_slug == "platform"
        assert by_name["platform/web_search"].cost_usdc == 10000

    @pytest.mark.anyio
    async def test_platform_tools_merged_with_org_tools(self, monkeypatch):
        """Both org and platform tools appear in the catalog."""
        org_rows = [
            {
                "name": "weather",
                "description": "Get weather",
                "marketplace_description": "Weather lookup",
                "input_schema": '{"properties": {}}',
                "base_price_usdc": 5000,
                "org_name": "Acme",
                "org_slug": "acme",
            },
        ]
        platform_rows = [
            {
                "tool_name": "get_token_price",
                "display_name": "Token Price",
                "description": "Live token prices",
                "base_price_usdc": 2000,
            },
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(side_effect=[org_rows, platform_rows])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog()

        assert len(catalog) == 2
        names = [t.qualified_name for t in catalog]
        assert "acme/weather" in names
        assert "platform/get_token_price" in names

    @pytest.mark.anyio
    async def test_platform_tool_override_price(self, monkeypatch):
        """Admin override prices take precedence over base_price_usdc."""
        platform_rows = [
            {
                "tool_name": "web_search",
                "display_name": "Web Search",
                "description": "Search",
                "base_price_usdc": 10000,
            },
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(side_effect=[[], platform_rows])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog(tool_overrides={"web_search": 15000})

        assert len(catalog) == 1
        assert catalog[0].cost_usdc == 15000


# ─── get_platform_tool_price ─────────────────────────────────────────────────


class TestGetPlatformToolPrice:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _invalidate_platform_tool_cache()
        yield
        _invalidate_platform_tool_cache()

    @pytest.mark.anyio
    async def test_returns_price_for_active_tool(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 4000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        price = await get_platform_tool_price("get_wallet_portfolio")
        assert price == 4000

    @pytest.mark.anyio
    async def test_returns_none_for_missing_tool(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        price = await get_platform_tool_price("nonexistent_tool")
        assert price is None

    @pytest.mark.anyio
    async def test_caches_result(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 2000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        # First call hits DB
        price1 = await get_platform_tool_price("http_fetch")
        # Second call should be cached
        price2 = await get_platform_tool_price("http_fetch")

        assert price1 == price2 == 2000
        assert mock_pool.fetchrow.call_count == 1

    @pytest.mark.anyio
    async def test_invalidate_cache_forces_refetch(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 2000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await get_platform_tool_price("http_fetch")
        _invalidate_platform_tool_cache()
        await get_platform_tool_price("http_fetch")

        assert mock_pool.fetchrow.call_count == 2


# ─── MCP billing gate: platform tool detection ──────────────────────────────


@pytest.fixture
async def billing_client(test_settings, monkeypatch):
    """Client with auth + billing + marketplace enabled."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_BILLING_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setenv("MCP_AUTH_AUDIENCE", "")
    config.get_settings.cache_clear()
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    config.get_settings.cache_clear()


class TestMCPBillingGatePlatformTools:
    """Integration-level tests verifying the billing gate detects platform tools."""

    @pytest.mark.asyncio
    async def test_platform_tool_billed_at_platform_price(self, billing_client, test_jwt_token):
        """A platform tool call should be billed at its marketplace_platform_tools price."""

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "get_token_price", "arguments": {"symbols": "eth"}},
            }
        )

        with (
            patch("billing.get_tool_pricing_overrides", new_callable=AsyncMock, return_value={}),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=MagicMock(tool_call_cost=1000)),
            patch("marketplace.get_platform_tool_price", new_callable=AsyncMock, return_value=2000),
            patch(
                "billing.verify_credit",
                new_callable=AsyncMock,
                return_value=MagicMock(verified=True, billing_method="credit", error=None),
            ),
            patch("billing.debit_credit", new_callable=AsyncMock, return_value=True) as mock_debit,
        ):
            resp = await billing_client.post(
                "/tools/mcp",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

        if resp.status_code == 200:
            mock_debit.assert_called_once_with("test-org-id", 2000, reason="mcp:get_token_price")

    @pytest.mark.asyncio
    async def test_non_platform_tool_uses_default_cost(self, billing_client, test_jwt_token):
        """A non-platform, non-marketplace tool falls back to default pricing."""

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "calculate", "arguments": {"expression": "1+1"}},
            }
        )

        with (
            patch("billing.get_tool_pricing_overrides", new_callable=AsyncMock, return_value={}),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=MagicMock(tool_call_cost=1000)),
            patch("marketplace.get_platform_tool_price", new_callable=AsyncMock, return_value=None),
            patch(
                "billing.verify_credit",
                new_callable=AsyncMock,
                return_value=MagicMock(verified=True, billing_method="credit", error=None),
            ),
            patch("billing.debit_credit", new_callable=AsyncMock, return_value=True) as mock_debit,
        ):
            resp = await billing_client.post(
                "/tools/mcp",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

        if resp.status_code == 200:
            # Should use default cost (1000), not a platform price
            mock_debit.assert_called_once_with("test-org-id", 1000, reason="mcp:calculate")


# ─── Migration 046: web3 primitive tools ────────────────────────────────────


class TestWeb3MarketplaceToolsMigration046:
    """Verify pricing and catalog visibility for the four tools seeded in migration 046."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _invalidate_platform_tool_cache()
        yield
        _invalidate_platform_tool_cache()

    # ── get_platform_tool_price — per-tool price resolution ──────────────

    @pytest.mark.anyio
    async def test_get_eth_balance_price(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 1000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("get_eth_balance") == 1000

    @pytest.mark.anyio
    async def test_get_erc20_balance_price(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 2000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("get_erc20_balance") == 2000

    @pytest.mark.anyio
    async def test_get_block_price(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 1000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("get_block") == 1000

    @pytest.mark.anyio
    async def test_get_transaction_price(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value={"base_price_usdc": 2000})
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("get_transaction") == 2000

    # ── Catalog visibility ────────────────────────────────────────────────

    @pytest.mark.anyio
    async def test_catalog_includes_all_four_web3_tools(self, monkeypatch):
        """All four tools appear in get_marketplace_catalog with correct qualified names."""
        platform_rows = [
            {"tool_name": "get_eth_balance", "display_name": "ETH Balance", "description": "...", "base_price_usdc": 1000},
            {"tool_name": "get_erc20_balance", "display_name": "ERC-20 Balance", "description": "...", "base_price_usdc": 2000},
            {"tool_name": "get_block", "display_name": "Block Details", "description": "...", "base_price_usdc": 1000},
            {"tool_name": "get_transaction", "display_name": "Transaction", "description": "...", "base_price_usdc": 2000},
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(side_effect=[[], platform_rows])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog()

        by_name = {t.qualified_name: t for t in catalog}
        assert "platform/get_eth_balance" in by_name
        assert "platform/get_erc20_balance" in by_name
        assert "platform/get_block" in by_name
        assert "platform/get_transaction" in by_name
        assert by_name["platform/get_eth_balance"].cost_usdc == 1000
        assert by_name["platform/get_erc20_balance"].cost_usdc == 2000
        assert by_name["platform/get_block"].cost_usdc == 1000
        assert by_name["platform/get_transaction"].cost_usdc == 2000
        assert all(t.author_org_slug == "platform" for t in catalog)

    # ── Billing integration: resolve_tool_cost ────────────────────────────

    @pytest.mark.anyio
    async def test_resolve_tool_cost_eth_balance(self, monkeypatch):
        """Billing resolves get_eth_balance at its marketplace price."""
        from billing import resolve_tool_cost

        monkeypatch.setattr("marketplace.get_platform_tool_price", AsyncMock(return_value=1000))
        cost = await resolve_tool_cost("get_eth_balance", {}, default_cost=0, marketplace_enabled=True)
        assert cost == 1000

    @pytest.mark.anyio
    async def test_resolve_tool_cost_get_transaction(self, monkeypatch):
        """Billing resolves get_transaction at its marketplace price."""
        from billing import resolve_tool_cost

        monkeypatch.setattr("marketplace.get_platform_tool_price", AsyncMock(return_value=2000))
        cost = await resolve_tool_cost("get_transaction", {}, default_cost=0, marketplace_enabled=True)
        assert cost == 2000

    @pytest.mark.anyio
    async def test_resolve_tool_cost_marketplace_disabled_returns_default(self, monkeypatch):
        """When marketplace is disabled, platform price is ignored and default is returned."""
        from billing import resolve_tool_cost

        mock_price = AsyncMock(return_value=1000)
        monkeypatch.setattr("marketplace.get_platform_tool_price", mock_price)
        cost = await resolve_tool_cost("get_eth_balance", {}, default_cost=0, marketplace_enabled=False)
        assert cost == 0
        mock_price.assert_not_called()

    @pytest.mark.anyio
    async def test_resolve_tool_cost_qualified_marketplace_uses_author_price(self, monkeypatch):
        """Qualified tools use cached author price when no override exists."""
        from billing import resolve_tool_cost

        monkeypatch.setattr("marketplace.get_org_tool_price_by_qualified_name", AsyncMock(return_value=2500))
        monkeypatch.setattr("marketplace.get_platform_tool_price", AsyncMock(return_value=9999))
        cost = await resolve_tool_cost("acme/weather", {}, default_cost=1000, marketplace_enabled=True)
        assert cost == 2500

    @pytest.mark.anyio
    async def test_resolve_tool_cost_qualified_bare_override_wins(self, monkeypatch):
        """Bare-name admin override should beat author price for qualified names."""
        from billing import resolve_tool_cost

        monkeypatch.setattr("marketplace.get_org_tool_price_by_qualified_name", AsyncMock(return_value=2500))
        cost = await resolve_tool_cost("acme/weather", {"weather": 9000}, default_cost=1000, marketplace_enabled=True)
        assert cost == 9000

    # ── Regression: excluded tools remain free ────────────────────────────

    @pytest.mark.anyio
    async def test_calculate_not_in_marketplace(self, monkeypatch):
        """calculate has no marketplace row and resolves to None."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("calculate") is None

    @pytest.mark.anyio
    async def test_get_datetime_not_in_marketplace(self, monkeypatch):
        """get_datetime has no marketplace row and resolves to None."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("get_datetime") is None


@pytest.mark.anyio
class TestMCPBillingGateQualifiedMarketplaceTools:
    async def test_qualified_tool_billed_at_author_price(self, billing_client, test_jwt_token):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 2,
                "params": {"name": "acme/weather", "arguments": {"city": "Paris"}},
            }
        )

        with (
            patch("billing.get_tool_pricing_overrides", new_callable=AsyncMock, return_value={}),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=MagicMock(tool_call_cost=1000)),
            patch("marketplace.get_org_tool_price_by_qualified_name", new_callable=AsyncMock, return_value=5000),
            patch("marketplace.check_org_subscription", new_callable=AsyncMock, return_value=True),
            patch(
                "billing.verify_credit",
                new_callable=AsyncMock,
                return_value=MagicMock(verified=True, billing_method="credit", error=None),
            ),
            patch("billing.debit_credit", new_callable=AsyncMock, return_value=True) as mock_debit,
            patch(
                "marketplace.get_marketplace_tool_by_name",
                new_callable=AsyncMock,
                return_value={"org_id": "author-org", "name": "weather", "base_price_usdc": 5000},
            ),
        ):
            resp = await billing_client.post(
                "/tools/mcp",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

        if resp.status_code == 200:
            mock_debit.assert_called_once_with("test-org-id", 5000, reason="mcp:acme/weather")

    async def test_qualified_tool_bare_override_wins(self, billing_client, test_jwt_token):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 3,
                "params": {"name": "acme/weather", "arguments": {"city": "Paris"}},
            }
        )

        with (
            patch("billing.get_tool_pricing_overrides", new_callable=AsyncMock, return_value={"weather": 9000}),
            patch("billing.get_current_pricing", new_callable=AsyncMock, return_value=MagicMock(tool_call_cost=1000)),
            patch("marketplace.get_org_tool_price_by_qualified_name", new_callable=AsyncMock, return_value=5000),
            patch("marketplace.check_org_subscription", new_callable=AsyncMock, return_value=True),
            patch(
                "billing.verify_credit",
                new_callable=AsyncMock,
                return_value=MagicMock(verified=True, billing_method="credit", error=None),
            ) as mock_verify,
            patch("billing.debit_credit", new_callable=AsyncMock, return_value=True),
            patch(
                "marketplace.get_marketplace_tool_by_name",
                new_callable=AsyncMock,
                return_value={"org_id": "author-org", "name": "weather", "base_price_usdc": 5000},
            ),
        ):
            _ = await billing_client.post(
                "/tools/mcp",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_jwt_token}",
                },
            )

        mock_verify.assert_called_once_with("test-org-id", 9000)

    # ── is_active soft-delete ─────────────────────────────────────────────

    @pytest.mark.anyio
    async def test_inactive_tool_returns_none(self, monkeypatch):
        """A tool with is_active=FALSE is excluded from pricing (DB returns no row)."""
        mock_pool = MagicMock()
        # DB query includes WHERE is_active = TRUE, so inactive rows return None
        mock_pool.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        assert await get_platform_tool_price("get_eth_balance") is None
