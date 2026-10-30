# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for marketplace REST endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

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
async def test_set_author_config_success_for_siwe_owner(api_client, monkeypatch):
    from types import SimpleNamespace

    from teardrop.auth import require_auth
    from teardrop.main import app

    siwe_address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

    async def _mock_siwe_auth():
        return {
            "sub": "siwe-user-id",
            "org_id": "test-org-id",
            "role": "user",
            "auth_method": "siwe",
            "address": siwe_address,
            "chain_id": 1,
        }

    app.dependency_overrides[require_auth] = _mock_siwe_auth
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        AsyncMock(return_value=SimpleNamespace(acquisition_source="siwe")),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        AsyncMock(return_value=SimpleNamespace(org_id="test-org-id", user_id="siwe-user-id")),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.set_author_config", AsyncMock(return_value=_AUTHOR_CONFIG))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/marketplace/author-config",
        json={"settlement_wallet": siwe_address.lower()},
    )

    assert resp.status_code == 200
    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_set_author_config_rejects_foreign_wallet_for_siwe_owner(api_client, monkeypatch):
    from types import SimpleNamespace

    from teardrop.auth import require_auth
    from teardrop.main import app

    siwe_address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    set_mock = AsyncMock(return_value=_AUTHOR_CONFIG)

    async def _mock_siwe_auth():
        return {
            "sub": "siwe-user-id",
            "org_id": "test-org-id",
            "role": "user",
            "auth_method": "siwe",
            "address": siwe_address,
            "chain_id": 1,
        }

    app.dependency_overrides[require_auth] = _mock_siwe_auth
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        AsyncMock(return_value=SimpleNamespace(acquisition_source="siwe")),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        AsyncMock(return_value=SimpleNamespace(org_id="test-org-id", user_id="siwe-user-id")),
    )
    monkeypatch.setattr("teardrop.routers.marketplace.set_author_config", set_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await api_client.post(
        "/marketplace/author-config",
        json={"settlement_wallet": "0x" + "2" * 40},
    )

    assert resp.status_code == 403
    set_mock.assert_not_awaited()
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
    rate_limit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", rate_limit_mock)
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", AsyncMock(return_value=withdrawal))

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await admin_api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"
    rate_limit_mock.assert_awaited_once_with(
        "marketplace:withdraw:test-org-id",
        config.get_settings().rate_limit_auth_rpm,
        detail="Rate limit exceeded for withdrawal requests.",
    )

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_request_withdrawal_forbidden_for_member(api_client, monkeypatch):
    """Non-admin members cannot move funds out of the org balance."""
    withdraw_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", withdraw_mock)

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 403
    withdraw_mock.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(("acquisition_source", "expected_status"), [("siwe", 201), ("x402", 201), ("email", 403), ("", 403)])
async def test_request_withdrawal_requires_owning_siwe_machine_org(api_client, monkeypatch, acquisition_source, expected_status):
    from types import SimpleNamespace

    from teardrop.auth import require_auth
    from teardrop.main import app

    siwe_address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    withdrawal = AuthorWithdrawal(
        id="w-2",
        org_id="test-org-id",
        amount_usdc=200_000,
        tx_hash="",
        wallet=_VALID_ADDR,
        status="pending",
        created_at=_NOW,
    )
    withdraw_mock = AsyncMock(return_value=withdrawal)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", withdraw_mock)

    async def _mock_siwe_auth():
        return {
            "sub": "siwe-user-id",
            "org_id": "test-org-id",
            "role": "user",
            "auth_method": "siwe",
            "address": siwe_address,
            "chain_id": 1,
        }

    app.dependency_overrides[require_auth] = _mock_siwe_auth
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        AsyncMock(return_value=SimpleNamespace(acquisition_source=acquisition_source)),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        AsyncMock(return_value=SimpleNamespace(org_id="test-org-id", user_id="siwe-user-id")),
    )

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == expected_status
    if expected_status == 201:
        withdraw_mock.assert_awaited_once_with("test-org-id", 200_000)
        assert resp.json()["wallet"] == _VALID_ADDR
    else:
        withdraw_mock.assert_not_awaited()

    app.dependency_overrides.pop(require_auth, None)


@pytest.mark.anyio
async def test_request_withdrawal_forbidden_for_machine_credentials(api_client, monkeypatch):
    """client_credentials tokens cannot move funds — payout requires the owning wallet."""
    from teardrop.auth import require_auth
    from teardrop.main import app

    withdraw_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", withdraw_mock)

    async def _mock_machine_auth():
        return {
            "sub": "client-id",
            "org_id": "test-org-id",
            "auth_method": "client_credentials",
        }

    app.dependency_overrides[require_auth] = _mock_machine_auth

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 403
    withdraw_mock.assert_not_awaited()

    app.dependency_overrides.pop(require_auth, None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("wallet_org", "wallet_user"),
    [("other-org", "siwe-user-id"), ("test-org-id", "other-user"), ("other-org", "other-user")],
)
async def test_request_withdrawal_forbidden_for_non_owning_siwe(api_client, monkeypatch, wallet_org, wallet_user):
    from types import SimpleNamespace

    from teardrop.auth import require_auth
    from teardrop.main import app

    siwe_address = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    withdraw_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", withdraw_mock)

    async def _mock_siwe_auth():
        return {
            "sub": "siwe-user-id",
            "org_id": "test-org-id",
            "role": "user",
            "auth_method": "siwe",
            "address": siwe_address,
            "chain_id": 1,
        }

    app.dependency_overrides[require_auth] = _mock_siwe_auth
    monkeypatch.setattr(
        "teardrop.users.get_org_by_id",
        AsyncMock(return_value=SimpleNamespace(acquisition_source="siwe")),
    )
    monkeypatch.setattr(
        "teardrop.wallets.get_wallet_by_address",
        AsyncMock(return_value=SimpleNamespace(org_id=wallet_org, user_id=wallet_user)),
    )

    resp = await api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 403
    withdraw_mock.assert_not_awaited()

    app.dependency_overrides.pop(require_auth, None)


@pytest.mark.anyio
async def test_request_withdrawal_rate_limited(admin_api_client, monkeypatch):
    """Withdrawal requests are org-scoped rate limited before any payout work."""
    from fastapi import HTTPException

    withdraw_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.request_withdrawal", withdraw_mock)
    monkeypatch.setattr(
        "teardrop.routers.marketplace._enforce_rate_limit",
        AsyncMock(side_effect=HTTPException(status_code=429, detail="Rate limit exceeded for withdrawal requests.")),
    )

    resp = await admin_api_client.post("/marketplace/withdraw", json={"amount_usdc": 200_000})
    assert resp.status_code == 429
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
        reputation_score=0.91,
        success_rate=0.96,
        unique_caller_count=17,
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
    assert data["tools"][0]["reputation_score"] == 0.91
    assert data["tools"][0]["success_rate"] == 0.96
    assert data["tools"][0]["unique_caller_count"] == 17
    assert data["tools"][0]["health_status"] == "healthy"
    assert data["tools"][0]["is_healthy"] is True
    assert resp.headers["cache-control"] == "public, max-age=60"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_marketplace_quote_uses_billing_resolver(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="my_tool",
        qualified_name="acme/my_tool",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=2500,
        author_org_name="Acme",
        author_org_slug="acme",
    )
    catalog_mock = AsyncMock(return_value=tool)
    resolver_mock = AsyncMock(return_value=2500)
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog_tool", catalog_mock)
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_live_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace.resolve_tool_cost", resolver_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/quote?tool=acme/my_tool")

    assert resp.status_code == 200
    body = resp.json()
    assert body["qualified_name"] == "acme/my_tool"
    assert body["price_usdc"] == 2500
    assert body["currency"] == "USDC"
    assert body["source"] == "marketplace"
    assert body["expires_at"].endswith("+00:00")
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert expires_at.tzinfo is not None
    assert (expires_at - datetime.now(timezone.utc)).total_seconds() <= config.get_settings().pricing_cache_ttl_seconds
    assert resp.headers["cache-control"] == "public, max-age=60"
    catalog_mock.assert_awaited_once_with("my_tool", "acme", {}, 0)
    resolver_mock.assert_awaited_once_with("acme/my_tool", {}, 0, marketplace_enabled=True)

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_marketplace_quote_normalizes_platform_name(anon_client, monkeypatch):
    tool = MarketplaceTool(
        name="get_eth_balance",
        qualified_name="platform/get_eth_balance",
        description="desc",
        marketplace_description="marketplace desc",
        input_schema={"type": "object"},
        cost_usdc=1000,
        author_org_name="Teardrop",
        author_org_slug="platform",
        tool_type="platform",
    )
    resolver_mock = AsyncMock(return_value=1000)
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog_tool", AsyncMock(return_value=tool))
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_live_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.marketplace.resolve_tool_cost", resolver_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/quote?tool=platform/get_eth_balance")

    assert resp.status_code == 200
    assert resp.json()["price_usdc"] == 1000
    resolver_mock.assert_awaited_once_with("get_eth_balance", {}, 0, marketplace_enabled=True)

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_marketplace_quote_rejects_invalid_or_unknown_tool(anon_client, monkeypatch):
    catalog_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog_tool", catalog_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    invalid = await anon_client.get("/marketplace/quote?tool=my_tool")
    assert invalid.status_code == 422
    catalog_mock.assert_not_awaited()

    unknown = await anon_client.get("/marketplace/quote?tool=acme/missing")
    assert unknown.status_code == 404
    catalog_mock.assert_awaited_once_with("missing", "acme", {}, 0)

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_delegation_quote_discloses_default_charge(anon_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/delegation/quote")

    assert resp.status_code == 200
    body = resp.json()
    s = config.get_settings()
    assert body["max_cost_usdc"] == s.a2a_delegation_max_cost_usdc == 100_000
    assert body["platform_fee_bps"] == s.a2a_delegation_platform_fee_bps == 500
    assert body["effective_max_charge_usdc"] == 105_000
    assert body["currency"] == "USDC"
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert expires_at.tzinfo is not None
    assert (expires_at - datetime.now(timezone.utc)).total_seconds() <= s.pricing_cache_ttl_seconds
    assert resp.headers["cache-control"] == "public, max-age=60"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_delegation_quote_reflects_overridden_cap(anon_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "2000000")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/delegation/quote")

    assert resp.status_code == 200
    body = resp.json()
    assert body["max_cost_usdc"] == 2_000_000
    assert body["platform_fee_bps"] == 500
    assert body["effective_max_charge_usdc"] == 2_100_000

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_delegation_quote_disabled_without_marketplace(anon_client, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ENABLED", raising=False)

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/delegation/quote")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Marketplace disabled."

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
async def test_catalog_q_param_passed_through(anon_client, monkeypatch):
    catalog_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog", catalog_mock)
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog?q=gpt")

    assert resp.status_code == 200
    catalog_mock.assert_awaited_once()
    assert catalog_mock.call_args.kwargs["q"] == "gpt"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_catalog_q_and_org_slug_params(anon_client, monkeypatch):
    catalog_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("teardrop.routers.marketplace.get_marketplace_catalog", catalog_mock)
    monkeypatch.setattr("teardrop.routers.marketplace.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/catalog?org_slug=acme&q=agent")

    assert resp.status_code == 200
    catalog_mock.assert_awaited_once()
    assert catalog_mock.call_args.kwargs["org_slug"] == "acme"
    assert catalog_mock.call_args.kwargs["q"] == "agent"

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_author_index_success(anon_client, monkeypatch):
    authors = [
        {"org_slug": "acme", "org_name": "Acme", "tool_count": 2, "total_calls": 7},
        {"org_slug": "platform", "org_name": "Teardrop", "tool_count": 12, "total_calls": 30},
    ]
    authors_mock = AsyncMock(return_value=authors)
    rate_limit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.marketplace.list_marketplace_authors_data", authors_mock)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", rate_limit_mock)
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/authors")

    assert resp.status_code == 200
    assert resp.json() == {"authors": authors, "next_cursor": None}
    assert resp.headers["cache-control"] == "public, max-age=60"
    authors_mock.assert_awaited_once_with(q=None, limit=100, cursor=None)
    rate_limit_mock.assert_awaited_once_with("catalog:127.0.0.1", config.get_settings().rate_limit_auth_rpm)

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_author_index_forwards_search_and_cursor(anon_client, monkeypatch):
    authors_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("teardrop.routers.marketplace.list_marketplace_authors_data", authors_mock)
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/authors?q=acme&limit=10&cursor=next")

    assert resp.status_code == 200
    authors_mock.assert_awaited_once_with(q="acme", limit=10, cursor="next")

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_author_index_emits_cursor_for_full_page(anon_client, monkeypatch):
    author = {"org_slug": "acme", "org_name": "Acme", "tool_count": 2, "total_calls": 7}
    monkeypatch.setattr(
        "teardrop.routers.marketplace.list_marketplace_authors_data",
        AsyncMock(return_value=[author]),
    )
    monkeypatch.setattr("teardrop.routers.marketplace._enforce_rate_limit", AsyncMock())
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")

    import teardrop.config as config

    config.get_settings.cache_clear()

    resp = await anon_client.get("/marketplace/authors?limit=1")

    assert resp.status_code == 200
    assert resp.json()["next_cursor"] == "eyJvcmdfc2x1ZyI6ICJhY21lIn0="

    config.get_settings.cache_clear()


@pytest.mark.anyio
async def test_author_index_rejects_q_too_long(anon_client):
    resp = await anon_client.get(f"/marketplace/authors?q={'a' * 201}")

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_catalog_rejects_q_too_long(anon_client):
    resp = await anon_client.get(f"/marketplace/catalog?q={'a' * 201}")

    assert resp.status_code == 422


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
    assert "marketplace desc" in resp.text
    assert "Description:" in resp.text
    assert "[Detail](http://test/marketplace/catalog/acme/my_tool)" in resp.text
    assert "[Quote](http://test/marketplace/quote?tool=acme/my_tool)" in resp.text
    assert "[Reputation](http://test/.well-known/reputation.json)" in resp.text
    assert "$1.234567" in resp.text

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
    monkeypatch.setattr("teardrop.routers.admin.marketplace.process_withdrawal", AsyncMock(return_value=withdrawal))

    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/w-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "w-1"
    assert data["status"] == "settled"


@pytest.mark.anyio
async def test_admin_process_withdrawal_not_found(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.admin.marketplace.process_withdrawal",
        AsyncMock(side_effect=ValueError("Withdrawal not found or not in 'pending' status")),
    )

    resp = await admin_api_client.post("/admin/marketplace/process-withdrawal/bad-id")
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_admin_complete_withdrawal_success(admin_api_client, monkeypatch):
    monkeypatch.setattr("teardrop.routers.admin.marketplace.complete_withdrawal", AsyncMock(return_value=None))

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
    monkeypatch.setattr("teardrop.routers.admin.marketplace.list_pending_withdrawals", AsyncMock(return_value=[withdrawal]))

    resp = await admin_api_client.get("/admin/marketplace/withdrawals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["withdrawals"]) == 1
    assert data["withdrawals"][0]["id"] == "w-1"


@pytest.mark.anyio
async def test_admin_list_withdrawals_org_filter(admin_api_client, monkeypatch):
    mock_list = AsyncMock(return_value=[])
    monkeypatch.setattr("teardrop.routers.admin.marketplace.list_pending_withdrawals", mock_list)

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


# ─── Admin pricing guard accepts qualified names ─────────────────────────────


@pytest.mark.anyio
async def test_admin_pricing_accepts_marketplace_tool(admin_api_client, monkeypatch):
    monkeypatch.setattr(
        "teardrop.routers.admin.billing.get_marketplace_tool_by_name",
        AsyncMock(return_value={"org_id": "o1", "name": "weather"}),
    )
    monkeypatch.setattr("teardrop.routers.admin.billing.upsert_tool_pricing_override", AsyncMock())

    resp = await admin_api_client.post(
        "/admin/pricing/tools",
        json={
            "tool_name": "acme/weather",
            "cost_usdc": 1_000_000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] is True


# ─── Price parity: catalog, quote, /tools/mcp and /agent/run share resolve_tool_cost ──


def _community_catalog_row() -> dict:
    return {
        "tool_id": None,
        "name": "weather",
        "qualified_name": "acme/weather",
        "display_name": "weather",
        "description": "Weather",
        "marketplace_description": "Weather lookup",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object", "properties": {"temp_c": {"type": "number"}}},
        "base_price_usdc": 1_000,
        "author_org_name": "Acme",
        "author_org_slug": "acme",
        "tool_type": "community",
        "category": "",
        "total_calls": 0,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(("listed_price", "expected"), [(None, 1_000), (0, 0), (5_000, 5_000)])
async def test_price_parity_across_surfaces(api_client, monkeypatch, listed_price, expected):
    """NULL = platform default, 0 = free, > 0 = author price — identical on every surface."""
    from billing import PricingRule, calculate_run_cost_usdc
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setenv("BILLING_ENABLED", "true")
    import teardrop.config as config

    config.get_settings.cache_clear()

    rule = PricingRule(id="rule-1", name="default", run_price_usdc=10_000, tool_call_cost=1_000)
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_community_catalog_row()])
    monkeypatch.setattr("marketplace._pool", pool)
    monkeypatch.setattr("marketplace.get_org_tool_price_by_qualified_name", AsyncMock(return_value=listed_price))
    for module in ("teardrop.routers.marketplace", "billing"):
        monkeypatch.setattr(f"{module}.get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr("teardrop.routers.marketplace.get_current_pricing", AsyncMock(return_value=rule))
    monkeypatch.setattr("teardrop.routers.marketplace.get_live_pricing", AsyncMock(return_value=rule))
    monkeypatch.setattr("billing.get_current_pricing", AsyncMock(return_value=rule))
    monkeypatch.setattr("billing.get_live_pricing", AsyncMock(return_value=rule))

    catalog = (await api_client.get("/marketplace/catalog")).json()["tools"][0]
    quote = (await api_client.get("/marketplace/quote?tool=acme/weather")).json()
    mcp_cost = await MCPGatewayMiddleware._resolve_tool_cost("acme/weather")
    run_cost = await calculate_run_cost_usdc({"billable_tool_calls": 1, "billable_tool_names": ["acme/weather"]})

    assert catalog["cost_usdc"] == quote["price_usdc"] == mcp_cost == run_cost == expected
    # Unrated tools publish null quality, not zero; declared output schemas are exposed.
    assert catalog["reputation_score"] is None
    assert catalog["success_rate"] is None
    assert catalog["output_schema"] == _community_catalog_row()["output_schema"]

    config.get_settings.cache_clear()
