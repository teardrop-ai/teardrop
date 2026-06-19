# SPDX-License-Identifier: BUSL-1.1
"""Tests for marketplace platform tools — catalog UNION and MCP billing gate."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import teardrop.config as config
from marketplace import (
    MarketplaceTool,
    PlatformToolSubscriptionError,
    _invalidate_platform_tool_cache,
    get_marketplace_catalog,
    get_platform_tool_price,
    subscribe_to_tool,
)

# ─── get_marketplace_catalog with platform tools ─────────────────────────────


class TestGetMarketplaceCatalogWithPlatformTools:
    @pytest.mark.anyio
    async def test_empty_org_tools_returns_platform_tools(self, monkeypatch):
        """When no org tools are published, catalog still returns platform tools."""
        mock_pool = MagicMock()
        platform_rows = [
            {
                "tool_id": None,
                "name": "web_search",
                "qualified_name": "platform/web_search",
                "display_name": "Web Search",
                "description": "Real-time web search",
                "marketplace_description": "Real-time web search",
                "input_schema": {},
                "base_price_usdc": 10000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "search",
                "total_calls": 9,
            },
            {
                "tool_id": None,
                "name": "http_fetch",
                "qualified_name": "platform/http_fetch",
                "display_name": "HTTP Fetch",
                "description": "SSRF-protected fetch",
                "marketplace_description": "SSRF-protected fetch",
                "input_schema": {},
                "base_price_usdc": 2000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "search",
                "total_calls": 3,
            },
        ]
        mock_pool.fetch = AsyncMock(return_value=platform_rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog()

        assert len(catalog) == 2
        assert all(isinstance(t, MarketplaceTool) for t in catalog)
        names = {t.qualified_name for t in catalog}
        assert "platform/web_search" in names
        assert "platform/http_fetch" in names
        by_name = {t.qualified_name: t for t in catalog}
        assert by_name["platform/http_fetch"].cost_usdc == 2000
        assert by_name["platform/http_fetch"].display_name == "HTTP Fetch"
        assert by_name["platform/http_fetch"].author_org_slug == "platform"
        assert by_name["platform/http_fetch"].tool_type == "platform"
        assert by_name["platform/http_fetch"].category == "search"
        assert by_name["platform/http_fetch"].total_calls == 3
        assert by_name["platform/http_fetch"].health_status == "healthy"
        assert by_name["platform/web_search"].cost_usdc == 10000
        assert by_name["platform/web_search"].tool_type == "platform"

    @pytest.mark.anyio
    async def test_platform_tools_merged_with_org_tools(self, monkeypatch):
        """Both org and platform tools appear in the catalog."""
        org_rows = [
            {
                "tool_id": None,
                "name": "weather",
                "qualified_name": "acme/weather",
                "display_name": "weather",
                "description": "Get weather",
                "marketplace_description": "Weather lookup",
                "input_schema": '{"properties": {}}',
                "base_price_usdc": 5000,
                "author_org_name": "Acme",
                "author_org_slug": "acme",
                "tool_type": "community",
                "category": "utility",
                "total_calls": 12,
            },
        ]
        platform_rows = [
            {
                "tool_id": None,
                "name": "get_token_price",
                "qualified_name": "platform/get_token_price",
                "display_name": "Token Price",
                "description": "Live token prices",
                "marketplace_description": "Live token prices",
                "input_schema": {},
                "base_price_usdc": 2000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "defi",
                "total_calls": 5,
            },
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=org_rows + platform_rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog()

        assert len(catalog) == 2
        names = [t.qualified_name for t in catalog]
        assert "acme/weather" in names
        assert "platform/get_token_price" in names
        by_name = {t.qualified_name: t for t in catalog}
        assert by_name["acme/weather"].tool_type == "community"
        assert by_name["acme/weather"].category == "utility"
        assert by_name["acme/weather"].total_calls == 12
        assert by_name["platform/get_token_price"].tool_type == "platform"

    @pytest.mark.anyio
    async def test_platform_tool_override_price(self, monkeypatch):
        """Admin override prices take precedence over base_price_usdc."""
        platform_rows = [
            {
                "tool_id": None,
                "name": "web_search",
                "qualified_name": "platform/web_search",
                "display_name": "Web Search",
                "description": "Search",
                "marketplace_description": "Search",
                "input_schema": {},
                "base_price_usdc": 10000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "search",
                "total_calls": 1,
            },
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=platform_rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        catalog = await get_marketplace_catalog(tool_overrides={"web_search": 15000})

        assert len(catalog) == 1
        assert catalog[0].cost_usdc == 15000

    @pytest.mark.anyio
    async def test_catalog_popularity_sort_and_category_filter(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await get_marketplace_catalog(category="defi", sort="popularity")

        sql = mock_pool.fetch.call_args.args[0]
        args = mock_pool.fetch.call_args.args[1:]
        assert "total_calls DESC" in sql
        assert "COALESCE(t.category, '')" in sql
        assert "COALESCE(p.category, '')" in sql
        assert "defi" in args


class TestGetMarketplaceCatalogSearch:
    @pytest.mark.anyio
    async def test_catalog_search_filters_across_catalog_fields(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await get_marketplace_catalog(q="GPT")

        sql = mock_pool.fetch.call_args.args[0]
        args = mock_pool.fetch.call_args.args[1:]
        assert "t.name ILIKE $1" in sql
        assert "t.marketplace_description ILIKE $1" in sql
        assert "o.slug ILIKE $1" in sql
        assert "p.display_name ILIKE $1" in sql
        assert "'Teardrop' ILIKE $1" in sql
        assert args[0] == "%GPT%"

    @pytest.mark.anyio
    async def test_catalog_search_intersects_with_org_slug(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await get_marketplace_catalog(org_slug="acme", q="GPT")

        sql = mock_pool.fetch.call_args.args[0]
        args = mock_pool.fetch.call_args.args[1:]
        assert "o.slug = $2" in sql
        assert "t.name ILIKE $1" in sql
        assert "FROM marketplace_platform_tools" not in sql
        assert args[0] == "%GPT%"
        assert args[1] == "acme"

    @pytest.mark.anyio
    async def test_catalog_search_escapes_like_wildcards(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await get_marketplace_catalog(q=r"100%_match")

        args = mock_pool.fetch.call_args.args[1:]
        assert args[0] == r"%100\%\_match%"

    @pytest.mark.anyio
    async def test_catalog_search_empty_string_skips_search_clause(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await get_marketplace_catalog(q="   ")

        sql = mock_pool.fetch.call_args.args[0]
        args = mock_pool.fetch.call_args.args[1:]
        assert "ILIKE" not in sql
        assert args == (100,)


# ─── get_platform_tool_price ─────────────────────────────────────────────────


class TestGetPlatformToolPrice:
    @pytest.fixture(autouse=True)
    async def _clear_cache(self):
        await _invalidate_platform_tool_cache()
        yield
        await _invalidate_platform_tool_cache()

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
        await _invalidate_platform_tool_cache()
        await get_platform_tool_price("http_fetch")

        assert mock_pool.fetchrow.call_count == 2


class TestPlatformToolSubscriptions:
    @pytest.mark.anyio
    async def test_platform_tool_subscription_rejected(self):
        with pytest.raises(PlatformToolSubscriptionError, match="always available without subscription"):
            await subscribe_to_tool("org-1", "platform/web_search")


# ─── MCP billing gate: platform tool detection ──────────────────────────────


@pytest.fixture
def billing_client(test_settings, monkeypatch):
    """Factory yielding a fresh mounted MCP app with marketplace billing enabled."""

    @asynccontextmanager
    async def _client():
        monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
        monkeypatch.setenv("MCP_BILLING_ENABLED", "true")
        monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
        monkeypatch.setenv("MCP_AUTH_AUDIENCE", "")
        config.get_settings.cache_clear()

        from teardrop.mcp_gateway import MCPGatewayMiddleware
        from tools.mcp_server import mcp

        mounted_mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
        mounted_app = FastAPI(lifespan=mounted_mcp_app.lifespan)
        mounted_app.add_middleware(MCPGatewayMiddleware)
        mounted_app.mount("/tools/mcp", mounted_mcp_app)

        try:
            async with mounted_app.router.lifespan_context(mounted_app):
                async with AsyncClient(transport=ASGITransport(app=mounted_app), base_url="http://test") as client:
                    yield client
        finally:
            config.get_settings.cache_clear()

    return _client


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

        async with billing_client() as client:
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
                resp = await client.post(
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

        async with billing_client() as client:
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
                resp = await client.post(
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
    async def _clear_cache(self):
        await _invalidate_platform_tool_cache()
        yield
        await _invalidate_platform_tool_cache()

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
            {
                "tool_id": None,
                "name": "get_eth_balance",
                "qualified_name": "platform/get_eth_balance",
                "display_name": "ETH Balance",
                "description": "...",
                "marketplace_description": "...",
                "input_schema": {},
                "base_price_usdc": 1000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "data",
                "total_calls": 0,
            },
            {
                "tool_id": None,
                "name": "get_erc20_balance",
                "qualified_name": "platform/get_erc20_balance",
                "display_name": "ERC-20 Balance",
                "description": "...",
                "marketplace_description": "...",
                "input_schema": {},
                "base_price_usdc": 2000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "data",
                "total_calls": 0,
            },
            {
                "tool_id": None,
                "name": "get_block",
                "qualified_name": "platform/get_block",
                "display_name": "Block Details",
                "description": "...",
                "marketplace_description": "...",
                "input_schema": {},
                "base_price_usdc": 1000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "data",
                "total_calls": 0,
            },
            {
                "tool_id": None,
                "name": "get_transaction",
                "qualified_name": "platform/get_transaction",
                "display_name": "Transaction",
                "description": "...",
                "marketplace_description": "...",
                "input_schema": {},
                "base_price_usdc": 2000,
                "author_org_name": "Teardrop",
                "author_org_slug": "platform",
                "tool_type": "platform",
                "category": "data",
                "total_calls": 0,
            },
        ]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=platform_rows)
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

    @pytest.mark.anyio
    async def test_resolve_tool_cost_mcp_tool_is_free(self, monkeypatch):
        """MCP tools (server__tool) are free unless explicitly overridden."""
        from billing import resolve_tool_cost

        mock_price = AsyncMock(return_value=2500)
        monkeypatch.setattr("marketplace.get_platform_tool_price", mock_price)

        cost = await resolve_tool_cost("github__list_repos", {}, default_cost=1000, marketplace_enabled=True)
        assert cost == 0
        mock_price.assert_not_called()

    @pytest.mark.anyio
    async def test_resolve_tool_cost_org_webhook_is_free_when_not_platform(self, monkeypatch):
        """Bare org webhook tools are free when absent from platform catalog."""
        from billing import resolve_tool_cost

        monkeypatch.setattr("marketplace.get_platform_tool_price", AsyncMock(return_value=None))
        cost = await resolve_tool_cost("my_crm_lookup", {}, default_cost=1000, marketplace_enabled=True)
        assert cost == 0

    @pytest.mark.anyio
    async def test_resolve_tool_cost_override_wins_for_mcp(self, monkeypatch):
        """Admin per-tool override must still win for MCP tools."""
        from billing import resolve_tool_cost

        mock_price = AsyncMock(return_value=2500)
        monkeypatch.setattr("marketplace.get_platform_tool_price", mock_price)

        cost = await resolve_tool_cost(
            "github__list_repos",
            {"github__list_repos": 500},
            default_cost=1000,
            marketplace_enabled=True,
        )
        assert cost == 500
        mock_price.assert_not_called()

    @pytest.mark.anyio
    async def test_qualified_marketplace_tool_with_double_underscore_in_name_is_billed(self, monkeypatch):
        """Regression: acme/my__tool must be billed at author price, not zeroed.

        Tool bare-names are ^[a-z][a-z0-9_]*$ which allows '__'.  The '__' MCP
        shortcut must not fire for qualified (slash-prefixed) marketplace names.
        """
        from billing import resolve_tool_cost

        monkeypatch.setattr(
            "marketplace.get_org_tool_price_by_qualified_name",
            AsyncMock(return_value=3500),
        )
        cost = await resolve_tool_cost("acme/my__tool", {}, default_cost=1000, marketplace_enabled=True)
        assert cost == 3500

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

        async with billing_client() as client:
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
                resp = await client.post(
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

        async with billing_client() as client:
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
                _ = await client.post(
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
