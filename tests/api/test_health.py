# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""API tests for / and /health endpoints."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from x402.schemas import PaymentRequirements

from billing.models import PricingRule


@pytest.fixture
def bootstrap_discovery_context(test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.machine_provisioning_enabled = True
    test_settings.x402_onboarding_enabled = True
    test_settings.credit_min_run_reserve_usdc = 50_000
    test_settings.app_base_url = "https://api.teardrop.dev"
    monkeypatch.setattr("teardrop.onboarding.settings", test_settings)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    pricing = PricingRule(id="test-discovery", name="test-discovery", run_price_usdc=10_000)
    monkeypatch.setattr("billing.get_live_pricing", AsyncMock(return_value=pricing))
    requirement = PaymentRequirements(
        scheme="exact",
        network="eip155:84532",
        asset="0x0000000000000000000000000000000000000000",
        amount="10000",
        pay_to="0x0000000000000000000000000000000000000001",
        max_timeout_seconds=300,
    )

    def requirements_for_amount(amount_usdc):
        return [requirement.model_copy(update={"amount": str(amount_usdc), "scheme": test_settings.x402_scheme})]

    monkeypatch.setattr("billing.x402.get_payment_requirements", lambda: requirements_for_amount(pricing.run_price_usdc))
    topup_mock = Mock(side_effect=requirements_for_amount)
    monkeypatch.setattr("billing.build_usdc_topup_requirements", topup_mock)
    return pricing, topup_mock


@pytest.mark.anyio
async def test_health_ok(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "teardrop"
    assert "version" in body
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["permissions-policy"] == "geolocation=(), microphone=()"


@pytest.mark.anyio
async def test_root_redirects_to_docs(api_client):
    resp = await api_client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/docs" in resp.headers.get("location", "")


@pytest.mark.anyio
async def test_a2a_async_headers_are_allowed_through_cors(api_client):
    resp = await api_client.options(
        "/message:send",
        headers={
            "Origin": "https://caller.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Prefer",
        },
    )

    assert resp.status_code == 200
    assert "prefer" in resp.headers["access-control-allow-headers"].lower()

    actual = await api_client.get("/health", headers={"Origin": "https://caller.example"})
    assert "location" in actual.headers["access-control-expose-headers"].lower()


@pytest.mark.anyio
async def test_agent_card_shape(api_client):
    resp = await api_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Teardrop"
    assert body["protocolVersion"] == "1.0"
    assert "skills" in body
    assert "tools" in body
    assert body["description"].startswith("A2A-delegable Web3 decision service")
    assert {tool["name"] for tool in body["tools"]} == {
        "assess_counterparty_risk",
        "validate_opportunity",
        "delegate_to_agent",
        "discover_agents",
    }
    assert "authentication" in body
    assert "securitySchemes" in body
    assert body["supportedInterfaces"][0]["url"] == "http://test/agent/run"
    assert body["supportedInterfaces"][1]["url"] == "http://test/message:send"
    assert body["defaultInputModes"] == ["text/plain", "application/json"]
    assert all("id" in skill for skill in body["skills"])
    assert all(skill["tags"] for skill in body["skills"])
    assert all(skill["examples"] for skill in body["skills"])
    assert {skill["id"] for skill in body["skills"]} == {
        "task_planning",
        "assess_counterparty_risk",
        "validate_opportunity",
        "delegate_to_agent",
        "discover_agents",
        "a2ui_rendering",
    }
    assert body["endpoints"]["a2a_message"] == "/message:send"
    assert body["endpoints"]["a2a_message_status"] == "/message:status/{task_id}"
    assert body["capabilities"]["asyncTasks"]["request_header"] == "Prefer: respond-async"
    assert body["endpoints"]["mcp_tools"] == "/tools/mcp"
    assert body["capabilities"]["billing"]["pricing_endpoint"] == "/billing/pricing"
    assert body["capabilities"]["onboarding"] == {
        "enabled": True,
        "methods": ["siwe"],
        "token_endpoint": "/token",
        "nonce_endpoint": "/auth/siwe/nonce",
        "topup_requirements_endpoint": "/billing/topup/usdc/requirements",
        "topup_endpoint": "/billing/topup/usdc",
        "credential_recovery": "siwe + POST /org/credentials/regenerate",
        "x402_grant_type": "x402",
    }


@pytest.mark.anyio
async def test_agent_card_advertises_x402_onboarding_when_enabled(api_client, test_settings):
    test_settings.billing_enabled = True
    test_settings.x402_onboarding_enabled = True

    response = await api_client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    assert response.json()["capabilities"]["onboarding"]["methods"] == ["siwe", "x402"]


@pytest.mark.anyio
async def test_agent_card_hides_onboarding_methods_when_disabled(api_client, test_settings):
    test_settings.machine_provisioning_enabled = False

    response = await api_client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    assert response.json()["capabilities"]["onboarding"]["methods"] == []


@pytest.mark.anyio
async def test_agent_card_advertises_event_trigger_control_plane(api_client, test_settings):
    test_settings.event_triggers_enabled = True

    response = await api_client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    body = response.json()
    capability = body["capabilities"]["eventTriggers"]
    assert capability["registration_endpoint"] == "/agent/event-triggers"
    assert capability["task_endpoint_template"].endswith("/{run_id}")
    assert capability["max_event_bytes"] == 64 * 1024
    assert body["endpoints"]["event_trigger_dispatch"] == "/agent/events/{trigger_token}"
    skill = next(item for item in body["skills"] if item["id"] == "event_trigger_ingress")
    assert "webhooks" in skill["tags"]
    assert "secret" not in body
    assert body["capabilities"]["pushNotifications"] is False


@pytest.mark.anyio
async def test_agent_card_hides_disabled_event_trigger_control_plane(api_client, test_settings):
    test_settings.event_triggers_enabled = False

    response = await api_client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    body = response.json()
    assert "eventTriggers" not in body["capabilities"]
    assert "event_trigger_ingress" not in {item["id"] for item in body["skills"]}


@pytest.mark.anyio
async def test_agent_card_includes_public_tool_reputation(api_client, monkeypatch):
    snapshot = {
        "generated_at": "2026-08-02T12:00:00+00:00",
        "tools": {
            "platform/assess_counterparty_risk": {
                "reputation_score": 0.93,
                "success_rate": 0.97,
            }
        },
    }
    monkeypatch.setattr(
        "teardrop.routers.system.get_public_reputation_snapshot",
        AsyncMock(return_value=snapshot),
    )

    response = await api_client.get("/.well-known/agent-card.json")

    risk_tool = next(tool for tool in response.json()["tools"] if tool["name"] == "assess_counterparty_risk")
    assert risk_tool["reputation"] == snapshot["tools"]["platform/assess_counterparty_risk"]


@pytest.mark.anyio
async def test_public_reputation_metadata_and_cache(api_client, monkeypatch):
    snapshot = {
        "generated_at": "2026-08-02T12:00:00+00:00",
        "tools": {
            "acme/oracle": {
                "reputation_score": 0.91,
                "success_rate": 0.96,
                "sample_size": 12.5,
                "confidence": 0.71,
                "freshness": 1.0,
                "average_latency_ms": 85.0,
            }
        },
    }
    monkeypatch.setattr(
        "teardrop.routers.system.get_public_reputation_snapshot",
        AsyncMock(return_value=snapshot),
    )

    response = await api_client.get("/.well-known/reputation.json")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "generated_at": snapshot["generated_at"],
        "methodology_url": "http://test/docs",
        "tools": [{"qualified_tool_name": "acme/oracle", **snapshot["tools"]["acme/oracle"]}],
    }
    assert "unique_caller_count" not in response.json()["tools"][0]
    assert response.headers["cache-control"] == "public, max-age=300"

    cached_response = await api_client.get(
        "/.well-known/reputation.json",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert cached_response.status_code == 304


@pytest.mark.anyio
async def test_registry_benefits_metadata_and_cache(api_client, test_settings):
    test_settings.marketplace_enabled = True

    response = await api_client.get("/.well-known/registry-benefits.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["schema_version"] == "1.0"
    assert body["registration"]["endpoint"] == "http://test/marketplace/agent-registration"
    assert body["registration"]["auth"] == ["org_admin", "org_bound_client_credentials"]
    assert {benefit["id"] for benefit in body["benefits"]} == {
        "directory_discovery",
        "planner_discovery",
        "outcome_reputation",
    }
    assert "guaranteed inbound calls or revenue" in body["does_not_provide"]

    cached_response = await api_client.get(
        "/.well-known/registry-benefits",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert cached_response.status_code == 304


@pytest.mark.anyio
async def test_registry_benefits_hidden_when_marketplace_disabled(api_client, test_settings):
    test_settings.marketplace_enabled = False

    response = await api_client.get("/.well-known/registry-benefits.json")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_agent_card_marketplace_discovery(api_client, test_settings):
    test_settings.marketplace_enabled = True

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["capabilities"]["marketplace"] == {
        "enabled": True,
        "catalog_endpoint": "/marketplace/catalog",
        "authors_endpoint": "/marketplace/authors",
        "agents_endpoint": "/marketplace/agents",
        "agent_directory_endpoint": "/marketplace/agents",
        "quote_endpoint": "/marketplace/quote?tool={qualified_name}",
        "author_catalog_endpoint": "/marketplace/catalog?org_slug={org_slug}",
        "self_inventory_endpoint": "/agent/tools",
        "agent_registration_endpoint": "/marketplace/agent-registration",
        "registration_benefits_endpoint": "/.well-known/registry-benefits.json",
        "mcp_gateway_endpoint": "/tools/mcp",
        "registration": {
            "author_config_endpoint": "/marketplace/author-config",
            "tool_registration_endpoint": "/tools",
            "auth": "siwe_or_admin",
            "wallet_binding": "siwe_self",
        },
    }
    assert body["endpoints"]["marketplace_catalog"] == "/marketplace/catalog"
    assert body["endpoints"]["marketplace_authors"] == "/marketplace/authors"
    assert body["endpoints"]["marketplace_quote"] == "/marketplace/quote"


@pytest.mark.anyio
async def test_agent_card_omits_marketplace_when_disabled(api_client, test_settings):
    test_settings.marketplace_enabled = False

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "marketplace" not in body["capabilities"]
    assert "marketplace_catalog" not in body["endpoints"]


@pytest.mark.anyio
async def test_agent_card_omits_inbound_a2a_when_disabled(api_client, test_settings):
    test_settings.a2a_inbound_enabled = False

    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["supportedInterfaces"] == [
        {
            "url": "http://test/agent/run",
            "protocolBinding": "https://teardrop.ai/bindings/ag-ui-sse/v1",
            "protocolVersion": "1.0",
        }
    ]
    assert "a2a_message" not in body["endpoints"]
    assert body["protocols"] == ["ag-ui", "mcp"]


@pytest.mark.anyio
async def test_agent_card_prefers_app_base_url(api_client, test_settings):
    test_settings.app_base_url = "https://api.teardrop.dev"

    resp = await api_client.get(
        "/.well-known/agent-card.json",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "ignored.example.com",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://api.teardrop.dev"
    assert body["documentationUrl"] == "https://api.teardrop.dev/docs"
    assert body["supportedInterfaces"][0]["url"] == "https://api.teardrop.dev/agent/run"
    assert body["supportedInterfaces"][1]["url"] == "https://api.teardrop.dev/message:send"


@pytest.mark.anyio
async def test_agent_card_falls_back_to_forwarded_host(api_client, test_settings):
    test_settings.app_base_url = ""

    resp = await api_client.get(
        "/.well-known/agent-card.json",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "proxy.teardrop.dev",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["url"] == "https://proxy.teardrop.dev"


@pytest.mark.anyio
async def test_agent_card_headers_and_legacy_alias(api_client):
    resp = await api_client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in resp.headers
    assert resp.headers["vary"] == "Host, X-Forwarded-Host, X-Forwarded-Proto"

    legacy_resp = await api_client.get("/.well-known/agent.json")
    assert legacy_resp.status_code == 200
    assert legacy_resp.json() == resp.json()

    cached_resp = await api_client.get(
        "/.well-known/agent-card.json",
        headers={"If-None-Match": resp.headers["etag"]},
    )
    assert cached_resp.status_code == 304


@pytest.mark.anyio
async def test_x402_discovery_metadata(api_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.mcp_x402_enabled = True
    test_settings.x402_onboarding_enabled = False
    monkeypatch.setattr(
        "teardrop.routers.system.build_402_response_body",
        lambda: {
            "error": "Payment required",
            "accepts": [{"scheme": "exact", "network": "eip155:8453", "maxAmountRequired": "0.01"}],
            "x402Version": 2,
        },
    )

    resp = await api_client.get("/.well-known/x402")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in resp.headers
    body = resp.json()
    assert body["x402Version"] == 2
    assert body["billing"] == {
        "enabled": True,
        "scheme": test_settings.x402_scheme,
        "network": test_settings.x402_network,
        "pricing_endpoint": "http://test/billing/pricing",
    }
    assert body["endpoints"]["a2a_message"] == "/message:send"
    assert body["endpoints"]["mcp_tools"] == "/tools/mcp"
    assert body["resources"][0]["path"] == "/agent/run"
    assert body["resources"][1] == {
        "path": "/message:send",
        "url": "http://test/message:send",
        "method": "POST",
        "protocol": "a2a",
        "auth_modes": ["bearer", "x402"],
        "description": "Blocking public A2A endpoint for external agent callers.",
    }
    assert body["resources"][2] == {
        "path": "/tools/mcp",
        "url": "http://test/tools/mcp",
        "method": "POST",
        "protocol": "mcp",
        "auth_modes": ["bearer", "x402"],
        "description": "MCP discovery and optional paid tool execution gateway.",
    }
    assert body["accepts"] == [{"scheme": "exact", "network": "eip155:8453", "maxAmountRequired": "0.01"}]

    json_resp = await api_client.get("/.well-known/x402.json")
    assert json_resp.status_code == 200
    assert json_resp.json() == body

    cached_resp = await api_client.get(
        "/.well-known/x402",
        headers={"If-None-Match": resp.headers["etag"]},
    )
    assert cached_resp.status_code == 304


@pytest.mark.anyio
@pytest.mark.parametrize("run_price", [10_000, 50_000, 75_000, 2**53 + 1])
@pytest.mark.parametrize("scheme", ["exact", "upto"])
async def test_x402_discovery_advertises_bootstrap_when_enabled(
    anon_client, test_settings, bootstrap_discovery_context, run_price, scheme
):
    pricing, _ = bootstrap_discovery_context
    pricing.run_price_usdc = run_price
    test_settings.x402_scheme = scheme
    expected_amount = max(run_price, test_settings.credit_min_run_reserve_usdc)

    resp, alias = await asyncio.gather(
        anon_client.get("/.well-known/x402", headers={"X-Forwarded-Host": "untrusted.example"}),
        anon_client.get("/.well-known/x402.json"),
    )
    challenge = await anon_client.post("/token", json={"grant_type": "x402"})

    assert resp.status_code == 200
    assert alias.status_code == 200
    assert challenge.status_code == 402
    body = resp.json()
    assert body == alias.json()
    assert resp.headers["etag"] == alias.headers["etag"]
    assert body["endpoints"]["bootstrap_token"] == "/token"
    assert body["bootstrap"] == {
        "grant_type": "x402",
        "token_endpoint": "/token",
        "amount_usdc": expected_amount,
        "accepts": challenge.json()["accepts"],
    }
    requirement = body["bootstrap"]["accepts"][0]
    assert requirement["amount"] == str(expected_amount)
    assert requirement["scheme"] == scheme
    assert requirement["payTo"] == "0x0000000000000000000000000000000000000001"
    assert requirement["maxTimeoutSeconds"] == 300
    assert "pay_to" not in requirement
    assert body["accepts"][0]["amount"] == str(run_price)
    bootstrap_resource = next(item for item in body["resources"] if item["path"] == "/token")
    assert bootstrap_resource["url"] == "https://api.teardrop.dev/token"
    assert bootstrap_resource["method"] == "POST"
    assert bootstrap_resource["protocol"] == "x402-bootstrap"
    assert bootstrap_resource["auth_modes"] == ["x402"]

    cached = await anon_client.get("/.well-known/x402.json", headers={"If-None-Match": resp.headers["etag"]})
    assert cached.status_code == 304
    pricing.run_price_usdc = expected_amount + 1
    changed = await anon_client.get("/.well-known/x402", headers={"If-None-Match": resp.headers["etag"]})
    assert changed.status_code == 200
    assert changed.headers["etag"] != resp.headers["etag"]
    assert changed.json()["bootstrap"]["amount_usdc"] == expected_amount + 1


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_requirements", [None, [], [{"amount": "50000"}]])
async def test_x402_discovery_rejects_invalid_bootstrap_requirements_and_recovers(
    anon_client, bootstrap_discovery_context, invalid_requirements
):
    _, topup_mock = bootstrap_discovery_context
    original_builder = topup_mock.side_effect
    topup_mock.side_effect = None
    topup_mock.return_value = invalid_requirements

    failed = await anon_client.get("/.well-known/x402")

    assert failed.status_code == 200
    assert failed.json()["bootstrap"] == {"grant_type": "x402", "token_endpoint": "/token", "accepts": []}
    assert failed.headers["cache-control"] == "public, max-age=0"
    topup_mock.side_effect = original_builder

    recovered = await anon_client.get("/.well-known/x402.json", headers={"If-None-Match": failed.headers["etag"]})

    assert recovered.status_code == 200
    assert recovered.json()["bootstrap"]["amount_usdc"] == 50_000
    assert recovered.json()["bootstrap"]["accepts"][0]["amount"] == "50000"


@pytest.mark.anyio
@pytest.mark.parametrize("disabled_flag", ["billing_enabled", "machine_provisioning_enabled", "x402_onboarding_enabled"])
async def test_x402_discovery_hides_bootstrap_when_disabled(anon_client, test_settings, monkeypatch, disabled_flag):
    test_settings.billing_enabled = True
    test_settings.machine_provisioning_enabled = True
    test_settings.x402_onboarding_enabled = True
    setattr(test_settings, disabled_flag, False)
    monkeypatch.setattr(
        "teardrop.routers.system.build_402_response_body",
        lambda: {"accepts": [], "x402Version": 2},
    )
    requirements_mock = AsyncMock()
    monkeypatch.setattr("teardrop.onboarding.get_bootstrap_payment_requirements", requirements_mock)

    resp = await anon_client.get("/.well-known/x402")

    assert resp.status_code == 200
    body = resp.json()
    assert "bootstrap" not in body
    assert "bootstrap_token" not in body["endpoints"]
    assert all(item["path"] != "/token" for item in body["resources"])
    requirements_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_discovery_survives_unavailable_bootstrap_requirements(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.machine_provisioning_enabled = True
    test_settings.x402_onboarding_enabled = True
    monkeypatch.setattr(
        "teardrop.routers.system.build_402_response_body",
        lambda: {"accepts": [], "x402Version": 2},
    )
    monkeypatch.setattr(
        "teardrop.onboarding.get_bootstrap_payment_requirements",
        AsyncMock(side_effect=RuntimeError("requirements unavailable")),
    )

    resp = await anon_client.get("/.well-known/x402")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=0"
    body = resp.json()
    assert body["bootstrap"] == {"grant_type": "x402", "token_endpoint": "/token", "accepts": []}
    assert body["endpoints"]["bootstrap_token"] == "/token"
    assert next(item for item in body["resources"] if item["path"] == "/token")


@pytest.mark.anyio
async def test_x402_discovery_redacts_bootstrap_errors(anon_client, test_settings, monkeypatch, caplog):
    test_settings.billing_enabled = True
    test_settings.machine_provisioning_enabled = True
    test_settings.x402_onboarding_enabled = True
    secret = "test-only-sensitive-payment-detail"
    monkeypatch.setattr("teardrop.routers.system.build_402_response_body", lambda: {"accepts": [], "x402Version": 2})
    monkeypatch.setattr(
        "teardrop.onboarding.get_bootstrap_payment_requirements",
        AsyncMock(side_effect=RuntimeError(secret)),
    )

    with caplog.at_level("DEBUG", logger="teardrop.routers.system"):
        response = await anon_client.get("/.well-known/x402")

    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/.well-known/x402", "/.well-known/x402.json"])
@pytest.mark.parametrize("pricing_ttl", [30, 0, -1, 600])
async def test_x402_discovery_bounds_cache_ttl(anon_client, test_settings, path, pricing_ttl):
    test_settings.billing_enabled = False
    test_settings.pricing_cache_ttl_seconds = pricing_ttl

    response = await anon_client.get(path)
    cached = await anon_client.get(path, headers={"If-None-Match": response.headers["etag"]})

    assert response.status_code == 200
    assert cached.status_code == 304
    expected = f"public, max-age={max(0, min(300, pricing_ttl))}"
    assert response.headers["cache-control"] == expected
    assert cached.headers["cache-control"] == expected


@pytest.mark.anyio
async def test_mcp_server_card(api_client, test_settings):
    test_settings.agent_card_icon_url = "https://example.com/icon.png"
    resp = await api_client.get("/.well-known/mcp/server-card.json")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in resp.headers

    body = resp.json()
    assert body["title"] == "Teardrop"
    assert body["description"].startswith("The native infrastructure layer")
    assert body["homepage"] == "http://test"
    assert body["documentationUrl"] == "http://test/docs"
    assert body["iconUrl"] == "https://example.com/icon.png"
    assert body["serverInfo"]["title"] == "Teardrop"
    assert body["serverInfo"]["websiteUrl"] == "http://test"
    assert body["serverInfo"]["icons"] == [{"src": "https://example.com/icon.png"}]

    if test_settings.mcp_x402_enabled:
        assert body["authentication"]["schemes"] == ["bearer", "x402"]
        x402 = body["x402"]
        assert x402["enabled"] is True
        assert x402["network"] == test_settings.x402_network
        assert x402["scheme"] == test_settings.x402_scheme
        assert x402["discovery_url"] == "http://test/.well-known/x402"
        assert x402["mcp_tools_url"] == "http://test/tools/mcp"
        assert x402["bootstrap"]["grant_type"] == "x402"
        assert x402["bootstrap"]["token_endpoint"] == "http://test/token"
    else:
        assert body["authentication"]["schemes"] == ["bearer"]
        assert "x402" not in body

    # Check that tools have outputSchema, annotations, title
    tools = body["tools"]
    assert len(tools) > 0
    tool_names = {tool["name"] for tool in tools}
    assert {"web_search", "get_wallet_portfolio"} <= tool_names
    t = tools[0]
    assert "title" in t
    assert "inputSchema" in t
    assert "outputSchema" in t
    assert "annotations" in t


@pytest.mark.anyio
async def test_oauth_protected_resource_metadata(api_client):
    root_resp = await api_client.get("/.well-known/oauth-protected-resource")

    assert root_resp.status_code == 200
    assert root_resp.headers["cache-control"] == "public, max-age=300"
    assert "etag" in root_resp.headers
    root_body = root_resp.json()
    assert root_body["resource"] == "http://test"
    assert root_body["resource_name"] == "Teardrop"
    assert root_body["resource_documentation"] == "http://test/docs"
    assert root_body["bearer_methods_supported"] == ["header"]
    assert root_body["homepage"] == "http://test"

    mcp_resp = await api_client.get("/.well-known/oauth-protected-resource/tools/mcp")

    assert mcp_resp.status_code == 200
    mcp_body = mcp_resp.json()
    assert mcp_body["resource"] == "http://test/tools/mcp"
    assert mcp_body["resource_name"] == "Teardrop MCP"
    assert mcp_body["resource_documentation"] == "http://test/docs"

    cached_resp = await api_client.get(
        "/.well-known/oauth-protected-resource/tools/mcp",
        headers={"If-None-Match": mcp_resp.headers["etag"]},
    )
    assert cached_resp.status_code == 304


@pytest.mark.anyio
async def test_root_llms_txt(api_client, test_settings):
    test_settings.marketplace_enabled = True

    resp = await api_client.get("/llms.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "# Teardrop" in resp.text
    assert "http://test/.well-known/agent-card.json" in resp.text
    assert "http://test/.well-known/reputation.json" in resp.text
    assert "http://test/.well-known/registry-benefits.json" in resp.text
    assert "http://test/marketplace/llms.txt" in resp.text


@pytest.mark.anyio
async def test_root_robots_txt(api_client):
    resp = await api_client.get("/robots.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "User-agent: *" in resp.text
    assert "http://test/llms.txt" in resp.text
