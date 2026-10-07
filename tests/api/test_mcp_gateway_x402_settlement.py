# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""End-to-end x402 settlement through the production-style mounted MCP gateway."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from x402.http import decode_payment_response_header

import teardrop.config as config


def _tools_call(tool_name: str = "calculate") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {"expression": "1+1"}},
    }


@pytest.fixture
def x402_gateway_env(test_settings, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_BILLING_ENABLED", "true")
    monkeypatch.setenv("MCP_X402_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _patch_billing(monkeypatch, *, tool_cost: int = 2_000):
    import billing

    scoped = [SimpleNamespace(scheme="exact", network="eip155:8453", amount=str(tool_cost))]
    verified = billing.BillingResult(
        verified=True,
        payer="0x" + "a" * 40,
        scheme="exact",
        payment_requirements=scoped[0],
    )
    settled = billing.BillingResult(
        verified=True,
        settled=True,
        tx_hash="0xtx",
        payer=verified.payer,
        amount_usdc=tool_cost,
        payment_requirements=verified.payment_requirements,
    )
    mocks = SimpleNamespace(
        scoped=scoped,
        build=lambda amount: scoped if amount == tool_cost else [],
        verify=AsyncMock(return_value=verified),
        settle=AsyncMock(return_value=settled),
        release=AsyncMock(),
        record=AsyncMock(),
        body=None,
    )
    original_body = billing.build_402_response_body

    def _body(**kwargs):
        mocks.body = kwargs
        return original_body(**{**kwargs, "requirements": []})

    monkeypatch.setattr(billing, "build_exact_payment_requirements", mocks.build)
    monkeypatch.setattr(billing, "build_402_response_body", _body)
    monkeypatch.setattr(billing, "build_402_headers", lambda **kwargs: {})
    monkeypatch.setattr(billing, "verify_payment", mocks.verify)
    monkeypatch.setattr(billing, "settle_payment", mocks.settle)
    monkeypatch.setattr(billing, "release_payment_nonce", mocks.release)
    monkeypatch.setattr(billing, "reserve_payer_spend", AsyncMock(return_value=True))
    monkeypatch.setattr(billing, "get_tool_pricing_overrides", AsyncMock(return_value={}))
    monkeypatch.setattr(billing, "get_current_pricing", AsyncMock(return_value=None))
    monkeypatch.setattr(billing, "resolve_tool_cost", AsyncMock(return_value=tool_cost))
    monkeypatch.setattr("teardrop.usage.record_mcp_call_event", mocks.record)
    monkeypatch.setattr("marketplace.record_marketplace_tool_usage_many", AsyncMock())
    return mocks


async def _post_paid_call(headers: dict[str, str], tool_name: str = "calculate"):
    from teardrop.mcp_gateway import MCPGatewayMiddleware, MCPPathNormalizer
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    app.add_middleware(MCPPathNormalizer)
    app.mount("/tools/mcp", MCPGatewayMiddleware(build_mcp_app(mcp), mounted=True))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/tools/mcp", json=_tools_call(tool_name), headers=headers)


@pytest.mark.asyncio
async def test_challenge_is_priced_at_tool_cost(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)

    response = await _post_paid_call({"Accept": "application/json"})

    assert response.status_code == 402
    assert mocks.body["requirements"] is mocks.scoped


@pytest.mark.asyncio
async def test_zero_cost_challenge_never_offers_upto(x402_gateway_env, monkeypatch):
    import billing

    mocks = _patch_billing(monkeypatch, tool_cost=0)
    upto = SimpleNamespace(scheme="upto", network="eip155:8453", amount="100000")
    exact = SimpleNamespace(scheme="exact", network="eip155:8453", amount="10000")
    monkeypatch.setattr(billing, "get_payment_requirements", lambda: [upto, exact])

    response = await _post_paid_call({"Accept": "application/json"}, tool_name="delegate_to_agent")

    assert response.status_code == 402
    assert mocks.body["requirements"] == [exact]


@pytest.mark.asyncio
async def test_allowlisted_zero_cost_tool_runs_free_for_anonymous_callers(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=0)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    hits: list[str] = []
    monkeypatch.setattr("teardrop.funnel_counters.record_discovery_hit", hits.append)

    response = await _post_paid_call({"Accept": "application/json", "X-PAYMENT": "signed-payment"})

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    mocks.verify.assert_not_awaited()
    mocks.settle.assert_not_awaited()
    mocks.record.assert_not_awaited()
    assert hits[-2:] == ["tools_call_free_anon", "tools_call_free_anon_client:script"]


@pytest.mark.asyncio
async def test_allowlisted_free_tool_is_ip_rate_limited(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=0)
    limiter = AsyncMock(return_value=(False, 0, 123))
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", limiter)
    hits: list[str] = []
    monkeypatch.setattr("teardrop.funnel_counters.record_discovery_hit", hits.append)

    response = await _post_paid_call({"Accept": "application/json"})

    assert response.status_code == 429
    assert limiter.await_args.args[0].startswith("mcp:ip:")
    mocks.verify.assert_not_awaited()
    assert "tools_call_free_anon" not in hits


async def _post_sequence(bodies: list[dict], headers: dict[str, str]) -> list:
    from teardrop.mcp_gateway import MCPGatewayMiddleware, MCPPathNormalizer
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    app.add_middleware(MCPPathNormalizer)
    app.mount("/tools/mcp", MCPGatewayMiddleware(build_mcp_app(mcp), mounted=True))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return [await client.post("/tools/mcp", json=body, headers=headers) for body in bodies]


def _initialize(client_name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": client_name}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("client_name", "expected"), [("smithery-probe", "bot"), ("claude-ai", "mcp")])
async def test_indexer_handshake_classifies_later_legacy_402_as_bot(x402_gateway_env, monkeypatch, client_name, expected):
    """A legacy tools/call has no clientInfo; the stateless server ties it to the handshake by IP."""
    from teardrop import funnel_counters

    _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))
    monkeypatch.setattr("teardrop.cache.get_redis", lambda: None)
    monkeypatch.setattr(funnel_counters, "_enabled", True)
    monkeypatch.setattr(funnel_counters, "_indexer_ips", {})
    hits: list[str] = []
    monkeypatch.setattr(funnel_counters, "record_discovery_hit", hits.append)

    responses = await _post_sequence(
        [_initialize(client_name), _tools_call("get_token_price")],
        {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18", "User-Agent": "node"},
    )

    assert responses[0].status_code == 200
    assert "mcp_402_no_payment" in hits
    assert hits[-1] == f"mcp_402_anon_client:{expected}"


async def _post_raw(content: bytes, headers: dict[str, str]):
    from teardrop.mcp_gateway import MCPGatewayMiddleware, MCPPathNormalizer
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    app.add_middleware(MCPPathNormalizer)
    app.mount("/tools/mcp", MCPGatewayMiddleware(build_mcp_app(mcp), mounted=True))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/tools/mcp", content=content, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"{}", b"", b'{"jsonrpc":"2.0","id":1}'])
async def test_non_jsonrpc_probe_gets_bazaar_402(x402_gateway_env, monkeypatch, content):
    from x402.extensions.bazaar.facilitator import validate_discovery_extension_spec

    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    response = await _post_raw(content, {"Content-Type": "application/json", "Accept": "application/json"})

    assert response.status_code == 402
    bazaar = mocks.body["extensions"]["bazaar"]
    assert validate_discovery_extension_spec(bazaar).valid
    assert bazaar["info"]["input"]["type"] == "http"
    assert bazaar["info"]["input"]["method"] == "POST"
    assert bazaar["info"]["input"]["body"]["params"]["name"] == "get_token_price"
    assert mocks.body["requirements"] is mocks.scoped
    assert mocks.body["resource"]["url"].endswith("/tools/mcp")
    mocks.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_jsonrpc_probe_with_bearer_is_not_a_payment_challenge(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    response = await _post_raw(b"{}", {"Content-Type": "application/json", "Authorization": "Bearer token"})

    assert response.status_code != 402
    assert mocks.body is None


@pytest.mark.asyncio
async def test_bazaar_example_call_is_a_paid_402(x402_gateway_env, monkeypatch):
    from teardrop.mcp_gateway import _ANON_FREE_TOOLS, _MCP_BAZAAR_INPUT_EXAMPLE

    _patch_billing(monkeypatch, tool_cost=2_000)

    response = await _post_raw(json.dumps(_MCP_BAZAAR_INPUT_EXAMPLE).encode(), {"Content-Type": "application/json"})

    assert _MCP_BAZAAR_INPUT_EXAMPLE["params"]["name"] not in _ANON_FREE_TOOLS
    assert response.status_code == 402


def _assert_full_service_metadata(resource: dict) -> None:
    from x402.extensions.bazaar.facilitator import _sanitize_resource_service_metadata
    from x402.schemas.payments import ResourceInfo

    from teardrop.bazaar_service import SERVICE_TAGS
    from teardrop.mcp_gateway import _BAZAAR_DESCRIPTION_MAX_CHARS

    # Facilitators soft-drop invalid fields, so every declared field must survive the SDK sanitizer.
    kept = _sanitize_resource_service_metadata(resource)
    assert kept.service_name == "Teardrop"
    assert kept.tags == list(SERVICE_TAGS)
    assert kept.icon_url == "https://teardrop.dev/teardrop.png"
    assert 0 < len(resource["description"]) <= _BAZAAR_DESCRIPTION_MAX_CHARS
    dumped = ResourceInfo.model_validate(resource).model_dump(by_alias=True, exclude_none=True)
    assert {"serviceName", "tags", "iconUrl"} <= dumped.keys()


@pytest.mark.asyncio
async def test_payment_probe_resource_carries_bazaar_service_metadata(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    response = await _post_raw(b"{}", {"Content-Type": "application/json"})

    assert response.status_code == 402
    _assert_full_service_metadata(mocks.body["resource"])
    assert response.json()["resource"]["serviceName"] == "Teardrop"


@pytest.mark.asyncio
async def test_tools_call_challenge_resource_carries_bazaar_service_metadata(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)

    response = await _post_paid_call({"Accept": "application/json"}, tool_name="get_token_price")

    assert response.status_code == 402
    _assert_full_service_metadata(mocks.body["resource"])
    assert mocks.body["extensions"]["bazaar"]["info"]["input"]["toolName"] == "get_token_price"


@pytest.mark.asyncio
async def test_non_http_icon_setting_is_omitted(x402_gateway_env, monkeypatch):
    monkeypatch.setenv("AGENT_CARD_ICON_URL", "")
    config.get_settings.cache_clear()
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    response = await _post_raw(b"{}", {"Content-Type": "application/json"})

    assert response.status_code == 402
    assert "iconUrl" not in mocks.body["resource"]
    assert mocks.body["resource"]["serviceName"] == "Teardrop"


async def _get_raw(headers: dict[str, str]):
    from teardrop.mcp_gateway import MCPGatewayMiddleware, MCPPathNormalizer
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    app.add_middleware(MCPPathNormalizer)
    app.mount("/tools/mcp", MCPGatewayMiddleware(build_mcp_app(mcp), mounted=True))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/tools/mcp", headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("accept", ["application/json", "*/*", "text/html"])
async def test_anonymous_non_sse_get_gets_bazaar_402(x402_gateway_env, monkeypatch, accept):
    from x402.extensions.bazaar.facilitator import validate_discovery_extension_spec

    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    limiter = AsyncMock(return_value=(True, 59, 0))
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", limiter)

    response = await _get_raw({"Accept": accept})

    assert response.status_code == 402
    assert validate_discovery_extension_spec(mocks.body["extensions"]["bazaar"]).valid
    assert mocks.body["requirements"] is mocks.scoped
    _assert_full_service_metadata(mocks.body["resource"])
    assert limiter.await_args.args[0].startswith("mcp:ip:")
    mocks.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_non_sse_get_is_ip_rate_limited(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(False, 0, 123)))

    response = await _get_raw({"Accept": "application/json"})

    assert response.status_code == 429
    assert mocks.body is None


@pytest.mark.asyncio
async def test_sse_get_still_reaches_mcp_transport(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    # An unsupported protocol version makes the SDK reject promptly instead of holding the SSE stream open.
    response = await _get_raw({"Accept": "text/event-stream", "mcp-protocol-version": "1999-01-01"})

    assert response.status_code not in (402, 406)
    assert mocks.body is None


@pytest.mark.asyncio
async def test_bearer_get_is_not_a_payment_challenge(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch, tool_cost=2_000)
    monkeypatch.setattr("teardrop.rate_limit._check_rate_limit", AsyncMock(return_value=(True, 59, 0)))

    response = await _get_raw({"Accept": "application/json", "Authorization": "Bearer token"})

    assert response.status_code == 406
    assert mocks.body is None


@pytest.mark.asyncio
async def test_verified_header_payment_settles_through_mounted_gateway(x402_gateway_env, monkeypatch):
    mocks = _patch_billing(monkeypatch)

    response = await _post_paid_call({"Accept": "application/json", "X-PAYMENT": "signed-payment"})

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    receipt = decode_payment_response_header(response.headers["payment-response"])
    assert (receipt.transaction, receipt.amount, receipt.network) == ("0xtx", "2000", "eip155:8453")
    assert response.headers["x-payment-response"] == response.headers["payment-response"]
    mocks.verify.assert_awaited_once_with("signed-payment", mocks.scoped)
    mocks.settle.assert_awaited_once()
    assert mocks.settle.await_args.kwargs["actual_cost_usdc"] == 2_000
    mocks.record.assert_awaited_once()
    assert mocks.record.await_args.args[5:7] == (2_000, "settled")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Accept": "application/json", "X-PAYMENT": "signed-payment"}, 402),
        ({"Accept": "application/json", "X-PAYMENT": "signed-payment", "mcp-protocol-version": "2025-11-25"}, 200),
    ],
)
async def test_unsettled_payment_withholds_tool_result(x402_gateway_env, monkeypatch, headers, status):
    import billing

    mocks = _patch_billing(monkeypatch)
    mocks.settle.return_value = billing.BillingResult(verified=True, settled=False, error="insufficient funds")

    response = await _post_paid_call(headers)

    assert response.status_code == status
    assert "payment-response" not in response.headers
    body = response.json()
    challenge = body if status == 402 else body["result"]["structuredContent"]
    assert "withheld" in challenge["error"]
    assert "result" not in body or body["result"]["isError"] is True
    assert '"2"' not in response.text
    mocks.release.assert_not_awaited()
    assert mocks.record.await_args.args[6] == "failed"


@pytest.mark.asyncio
async def test_verified_payment_is_released_when_billing_disabled(x402_gateway_env, monkeypatch):
    monkeypatch.setenv("MCP_BILLING_ENABLED", "false")
    config.get_settings.cache_clear()
    mocks = _patch_billing(monkeypatch)

    response = await _post_paid_call({"Accept": "application/json", "X-PAYMENT": "signed-payment"})

    assert response.status_code == 503
    assert "error" in json.loads(response.text)
    mocks.settle.assert_not_awaited()
    mocks.release.assert_awaited_once_with("signed-payment")
