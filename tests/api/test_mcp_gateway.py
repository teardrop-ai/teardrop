# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Tests for the MCP gateway — Phase 1: JWKS endpoint + JWT auth gate."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

import teardrop.config as config  # ── JWKS endpoint ─────────────────────────────────────────────────────────────
from teardrop._meta import APP_VERSION


@pytest.fixture(autouse=True)
def _flat_x402_pricing(monkeypatch):
    # Scoped per-tool pricing is covered in test_mcp_gateway_x402_settlement.py.
    monkeypatch.setattr(
        "teardrop.mcp_gateway.MCPGatewayMiddleware._x402_tool_requirements",
        AsyncMock(return_value=None),
    )


def test_mcp_bazaar_extension_has_valid_flat_jsonrpc_schema():
    from x402.extensions.bazaar import validate_discovery_extension

    from teardrop.mcp_gateway import _mcp_402_extensions

    bazaar = _mcp_402_extensions()["bazaar"]
    body_schema = bazaar["schema"]["properties"]["input"]["properties"]["body"]

    result = validate_discovery_extension(bazaar)

    assert result.valid, result.errors
    assert bazaar["info"]["input"]["method"] == "POST"
    assert body_schema["properties"]["method"]["const"] == "tools/call"
    assert "$defs" not in json.dumps(body_schema)
    assert "$ref" not in json.dumps(body_schema)


@pytest.mark.parametrize("tool_name", ["get_token_price", "calculate", "assess_counterparty_risk"])
def test_mcp_bazaar_extension_declares_called_mcp_tool(tool_name):
    from x402.extensions.bazaar import validate_discovery_extension
    from x402.extensions.bazaar.facilitator import validate_discovery_extension_spec

    from teardrop.mcp_gateway import _mcp_402_extensions

    bazaar = _mcp_402_extensions(tool_name)["bazaar"]
    mcp_input = bazaar["info"]["input"]

    assert validate_discovery_extension(bazaar).valid
    assert validate_discovery_extension_spec(bazaar).valid
    assert mcp_input["type"] == "mcp"
    assert mcp_input["toolName"] == tool_name
    assert mcp_input["transport"] == "streamable-http"
    assert mcp_input["inputSchema"]["type"] == "object"
    assert "$ref" not in json.dumps(bazaar)
    assert "$defs" not in json.dumps(bazaar)


def test_mcp_bazaar_extension_falls_back_for_unknown_tool():
    from teardrop.mcp_gateway import _mcp_402_extensions

    assert _mcp_402_extensions("not_a_registered_tool")["bazaar"]["info"]["input"]["type"] == "http"


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_header", [None, "invalid-payment"])
async def test_mcp_x402_challenges_include_bazaar_in_body_and_headers(monkeypatch, payment_header):
    import billing
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    headers = [] if payment_header is None else [(b"payment-signature", payment_header.encode())]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("test", 443),
            "path": "/tools/mcp",
            "headers": headers,
        }
    )
    seen: dict[str, dict] = {}

    def _body(**kwargs):
        seen["body"] = kwargs
        return {
            "error": kwargs.get("error", "Payment required"),
            "accepts": [],
            "x402Version": 2,
            "resource": kwargs["resource"],
            "extensions": kwargs["extensions"],
        }

    def _headers(**kwargs):
        seen["headers"] = kwargs
        return {"PAYMENT-REQUIRED": "encoded", "X-PAYMENT-REQUIRED": "legacy"}

    monkeypatch.setattr(billing, "build_402_response_body", _body)
    monkeypatch.setattr(billing, "build_402_headers", _headers)
    verify_mock = AsyncMock(return_value=SimpleNamespace(verified=False, error="Invalid payment"))
    monkeypatch.setattr(billing, "verify_payment", verify_mock)

    response = await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(request)

    assert response is not None
    assert response.status_code == 402
    body = json.loads(response.body)
    assert body["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"
    assert body["resource"]["url"] == "https://test/tools/mcp"
    assert seen["headers"]["extensions"] == seen["body"]["extensions"]
    assert "org_id" not in json.dumps(body)
    if payment_header is None:
        verify_mock.assert_not_awaited()
        assert body["error"] == "Payment required"
    else:
        verify_mock.assert_awaited_once_with(payment_header, None)
        assert body["error"] == "Invalid payment"


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_header", [None, "invalid-payment"])
async def test_mcp_x402_challenge_records_funnel_counter(monkeypatch, payment_header):
    import billing
    import teardrop.funnel_counters as funnel_module
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    funnel_module.init_funnel_counters(None, enabled=True)
    try:
        headers = [] if payment_header is None else [(b"payment-signature", payment_header.encode())]
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "server": ("test", 443),
                "path": "/tools/mcp",
                "headers": headers,
            }
        )

        monkeypatch.setattr(billing, "build_402_response_body", lambda **kwargs: {"error": "Payment required"})
        monkeypatch.setattr(billing, "build_402_headers", lambda **kwargs: {})
        monkeypatch.setattr(
            billing,
            "verify_payment",
            AsyncMock(return_value=SimpleNamespace(verified=False, error="Invalid payment")),
        )

        response = await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(request)

        assert response is not None
        assert response.status_code == 402
        subtype = (
            funnel_module.SURFACE_MCP_402_NO_PAYMENT if payment_header is None else funnel_module.SURFACE_MCP_402_PAYMENT_INVALID
        )
        assert sum(funnel_module._counters.values()) == 2
        assert {surface for surface, _ in funnel_module._counters} == {
            funnel_module.SURFACE_MCP_402_CHALLENGE,
            subtype,
        }
    finally:
        funnel_module.close_funnel_counters()


# ── x402 MCP transport (params._meta payments, result-level payment-required) ──

_BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _mcp_request(body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("test", 443),
            "path": "/tools/mcp",
            "headers": headers or [],
        },
        receive,
    )


def _real_402_body(monkeypatch):
    from x402.schemas import PaymentRequirements

    import billing

    requirement = PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset=_BASE_USDC,
        amount="10000",
        pay_to="0x" + "1" * 40,
        max_timeout_seconds=300,
        extra={"name": "USD Coin", "version": "2"},
    )
    original = billing.build_402_response_body
    monkeypatch.setattr(billing, "build_402_response_body", lambda **kw: original(requirements=[requirement], **kw))


def _tools_call(**params) -> dict:
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "calculate", "arguments": {}, **params}}


def test_mcp_payment_meta_keys_match_x402_sdk():
    from x402.mcp.types import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY

    from teardrop import mcp_gateway

    assert mcp_gateway._MCP_PAYMENT_META_KEY == MCP_PAYMENT_META_KEY
    assert mcp_gateway._MCP_PAYMENT_RESPONSE_META_KEY == MCP_PAYMENT_RESPONSE_META_KEY


def test_meta_payment_header_is_canonical():
    from teardrop.mcp_gateway import _meta_payment_header

    as_dict = _tools_call(_meta={"x402/payment": {"b": 1, "a": {"y": 2, "x": 1}}})
    as_json = _tools_call(_meta={"x402/payment": json.dumps({"a": {"x": 1, "y": 2}, "b": 1}, indent=2)})

    assert _meta_payment_header(as_dict) == _meta_payment_header(as_json)
    assert json.loads(base64.b64decode(_meta_payment_header(as_dict))) == {"a": {"x": 1, "y": 2}, "b": 1}
    assert _meta_payment_header(_tools_call()) is None
    assert _meta_payment_header({"params": "not-a-dict"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [[(b"accept", b"application/json, text/event-stream")], [(b"mcp-protocol-version", b"2025-06-18")]],
)
async def test_mcp_client_gets_result_level_payment_required(monkeypatch, headers):
    from x402.mcp.types import MCPToolResult
    from x402.mcp.utils import extract_payment_required_from_result

    from teardrop.mcp_gateway import MCPGatewayMiddleware

    _real_402_body(monkeypatch)
    response = await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(_mcp_request(_tools_call(), headers))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["id"] == 7
    result = body["result"]
    assert result["isError"] is True
    assert "POST /token" in result["content"][1]["text"]
    payment_required = extract_payment_required_from_result(
        MCPToolResult(
            content=result["content"],
            is_error=result["isError"],
            structured_content=result["structuredContent"],
        )
    )
    assert payment_required is not None
    assert payment_required.accepts[0].network == "eip155:8453"
    assert payment_required.accepts[0].amount == "10000"


@pytest.mark.asyncio
async def test_plain_http_client_keeps_http_402(monkeypatch):
    import billing
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    _real_402_body(monkeypatch)
    monkeypatch.setattr(billing, "build_402_headers", lambda **kwargs: {"PAYMENT-REQUIRED": "encoded"})
    response = await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(
        _mcp_request(_tools_call(), [(b"accept", b"application/json")])
    )

    assert response.status_code == 402
    assert response.headers["payment-required"] == "encoded"
    assert json.loads(response.body)["accepts"][0]["amount"] == "10000"


@pytest.mark.asyncio
async def test_meta_payment_is_verified_and_failure_uses_mcp_signal(monkeypatch):
    import billing
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    _real_402_body(monkeypatch)
    verify_mock = AsyncMock(return_value=SimpleNamespace(verified=False, error="Payment verification failed: bad"))
    monkeypatch.setattr(billing, "verify_payment", verify_mock)
    payment = {"x402Version": 2, "payload": {"signature": "0xabc"}}

    response = await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(_mcp_request(_tools_call(_meta={"x402/payment": payment})))

    assert json.loads(base64.b64decode(verify_mock.await_args.args[0])) == payment
    assert response.status_code == 200
    result = json.loads(response.body)["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "Payment verification failed: bad"


@pytest.mark.asyncio
async def test_verified_meta_payment_feeds_reservation_header(monkeypatch):
    import billing
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    monkeypatch.setattr(
        billing,
        "verify_payment",
        AsyncMock(return_value=SimpleNamespace(verified=True, payer="0xpayer")),
    )
    request = _mcp_request(_tools_call(_meta={"x402/payment": {"x402Version": 2}}))

    assert await MCPGatewayMiddleware(FastAPI())._handle_x402_auth(request) is None
    assert request.state.mcp_auth_method == "x402"
    assert MCPGatewayMiddleware._payment_header(request) == request.state.mcp_x402_payment


@pytest.mark.asyncio
async def test_settle_billing_attaches_receipt_for_meta_payment(monkeypatch):
    from starlette.responses import StreamingResponse
    from x402.mcp.types import MCPToolResult
    from x402.mcp.utils import extract_payment_response_from_meta

    from billing import BillingResult
    from teardrop.mcp_gateway import MCPGatewayMiddleware

    gateway = MCPGatewayMiddleware(FastAPI())
    tool_body = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "2"}], "isError": False}}
    response = StreamingResponse(iter([json.dumps(tool_body).encode()]), media_type="application/json")
    settled = BillingResult(
        verified=True,
        settled=True,
        tx_hash="0xtx",
        payer="0xpayer",
        amount_usdc=10000,
        payment_requirements=SimpleNamespace(network="eip155:8453"),
    )
    request = _mcp_request(_tools_call())
    request.state.x402_billing = settled
    request.state.mcp_x402_payment = "encoded-meta-payment"
    monkeypatch.setattr("billing.settle_payment", AsyncMock(return_value=settled))
    monkeypatch.setattr(gateway, "_record_mcp_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr("teardrop.mcp_gateway.asyncio.create_task", lambda coro: coro.close())

    result = await gateway._settle_billing(request, (None, 10000, "calculate", 7), response)

    body = json.loads(result.body)
    assert result.headers["content-length"] == str(len(result.body))
    receipt = extract_payment_response_from_meta(MCPToolResult(content=body["result"]["content"], meta=body["result"]["_meta"]))
    assert receipt is not None
    assert receipt.success is True
    assert receipt.transaction == "0xtx"
    assert receipt.network == "eip155:8453"
    assert body["result"]["content"][0]["text"] == "2"


@pytest.mark.asyncio
async def test_jwks_returns_valid_key(test_settings):
    """GET /.well-known/jwks.json returns a valid RSA JWK."""
    from teardrop.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/.well-known/jwks.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) == 1

    key = body["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["use"] == "sig"
    assert key["kid"] == "teardrop-rs256"
    assert len(key["n"]) > 10  # non-trivial modulus
    assert key["e"]  # non-empty exponent


# ── Auth gate ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def mcp_client(test_settings, monkeypatch):
    """AsyncClient with mcp_auth_enabled=True and no dep overrides."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    # Match render.yaml so unscoped /token JWTs are exercised against the production audience.
    monkeypatch.setenv("MCP_AUTH_AUDIENCE", "teardrop-mcp")
    config.get_settings.cache_clear()
    from teardrop.main import app

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        yield c
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_gate_rejects_missing_token(mcp_client):
    """POST /tools/mcp without Authorization header executing tools/call → 401."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.asyncio
async def test_auth_gate_rejects_invalid_token(mcp_client):
    """POST /tools/mcp with garbage Bearer executing tools/call → 401."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer not-a-valid-jwt",
        },
    )
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_auth_gate_rejects_expired_token(mcp_client, test_settings):
    """POST /tools/mcp with an expired JWT executing tools/call → 401."""
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt

    payload = {
        "sub": "test-user",
        "iss": test_settings.jwt_issuer,
        "iat": datetime.now(timezone.utc) - timedelta(hours=2),
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        "org_id": "test-org",
    }
    expired_token = pyjwt.encode(payload, test_settings.jwt_private_key, algorithm="RS256")

    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {expired_token}",
        },
    )
    assert resp.status_code == 401
    assert "token_expired" in resp.headers.get("WWW-Authenticate", "")


@pytest.mark.asyncio
async def test_auth_gate_passes_valid_token(mcp_client, test_jwt_token):
    """POST /tools/mcp with valid JWT executing tools/call passes through to MCPServer."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {test_jwt_token}",
        },
    )
    # MCPServer handles the request — may be 200 or a JSON-RPC error, but NOT 401.
    assert resp.status_code != 401


@pytest.mark.asyncio
@pytest.mark.parametrize(("aud", "expected_401"), [("teardrop-mcp", False), ("other-app", True)])
async def test_auth_gate_enforces_audience_on_scoped_tokens(mcp_client, test_settings, aud, expected_401):
    from teardrop.auth import create_access_token

    token = create_access_token("test-user-id", extra_claims={"org_id": "test-org-id", "aud": aud})
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    if expected_401:
        assert resp.status_code == 401
        assert "invalid_audience" in resp.headers.get("WWW-Authenticate", "")
    else:
        assert resp.status_code != 401


@pytest.mark.asyncio
async def test_auth_gate_disabled_passes_through(test_settings, monkeypatch):
    """When mcp_auth_enabled=False, unauthenticated tools/call passes through."""
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    config.get_settings.cache_clear()
    from teardrop.main import app

    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        resp = await c.post(
            "/tools/mcp",
            content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code != 401
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_non_mcp_path_not_intercepted(mcp_client):
    """Middleware does not affect non-MCP paths like /health."""
    resp = await mcp_client.get("/health")
    # /health should work without auth regardless of mcp_auth_enabled.
    assert resp.status_code in (200, 503)  # ok or degraded (no DB in tests)


@pytest.mark.asyncio
async def test_mcp_app_real_handshake():
    """Verify MCPServer stateless streamable layer returns a valid initialize handshake."""
    from teardrop.app import mcp_app

    async with mcp_app.router.lifespan_context(mcp_app):
        # We test MCPServer's ASGI app directly here because testing it through proxy
        # requires the full FastAPI DB-connected lifespan.
        async with AsyncClient(
            transport=ASGITransport(app=mcp_app), base_url="http://test", headers={"Accept": "application/json"}
        ) as c:
            resp = await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "result" in data
            assert data["result"]["protocolVersion"] == "2024-11-05"
            assert data["result"]["serverInfo"]["name"] == "Teardrop"
            assert data["result"]["serverInfo"]["version"] == APP_VERSION

            # Check tools/list is also available
            resp_list = await c.post(
                "/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert resp_list.status_code == 200
            list_data = resp_list.json()
            assert "tools" in list_data["result"]
            assert len(list_data["result"]["tools"]) > 0
            calculate_tool = next(tool for tool in list_data["result"]["tools"] if tool["name"] == "calculate")
            assert "outputSchema" in calculate_tool
            assert "description" in calculate_tool["inputSchema"]["properties"]["expression"]


@pytest.mark.asyncio
async def test_mcp_tool_reputation_reaches_http_clients(monkeypatch):
    from tools.mcp_server import build_mcp_app, create_mcp_server, refresh_mcp_tool_reputations

    server = create_mcp_server()
    monkeypatch.setattr(
        "marketplace.reputation.get_public_reputation",
        AsyncMock(return_value={"platform/calculate": {"reputation_score": 0.9, "sample_size": 0.5}}),
    )
    await refresh_mcp_tool_reputations(server)
    mcp_app = build_mcp_app(server)

    async with mcp_app.router.lifespan_context(mcp_app):
        async with AsyncClient(
            transport=ASGITransport(app=mcp_app), base_url="http://test", headers={"Accept": "application/json"}
        ) as client:
            response = await client.post("/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert response.status_code == 200
    tool = next(tool for tool in response.json()["result"]["tools"] if tool["name"] == "calculate")
    assert "Observed quality: score=0.90, sample_size=0.5." in tool["description"]
    assert tool["_meta"] == {"teardrop/reputation": {"reputation_score": 0.9, "sample_size": 0.5}}


@pytest.mark.asyncio
async def test_mounted_mcp_normalizes_no_slash_path():
    """POST /tools/mcp should hit the mounted MCP app, not /tools/{tool_id}."""
    from teardrop.mcp_gateway import MCPGatewayMiddleware
    from tools.mcp_server import build_mcp_app, create_mcp_server

    mcp = create_mcp_server()
    mounted_mcp_app = build_mcp_app(mcp)
    mounted_app = FastAPI(lifespan=lambda _: mcp.session_manager.run())
    mounted_app.add_middleware(MCPGatewayMiddleware)
    mounted_app.mount("/tools/mcp", mounted_mcp_app)

    async with mounted_app.router.lifespan_context(mounted_app):
        async with AsyncClient(
            transport=ASGITransport(app=mounted_app),
            base_url="http://test",
            headers={"Accept": "application/json"},
        ) as c:
            resp = await c.post(
                "/tools/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["serverInfo"]["name"] == "Teardrop"


@pytest.mark.asyncio
async def test_public_discovery_bypass_success(mcp_client):
    """POST /tools/mcp with initialize method bypasses auth for all clients."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1}),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "RandomClient/1.0",
        },
    )
    # Bypasses 401/402 gate completely and should not fall through to 405.
    # ASGITransport still skips the full app lifespan, so a 500 is acceptable here.
    assert resp.status_code not in (401, 402, 405)


@pytest.mark.asyncio
async def test_smithery_events_list_returns_empty_catalog(mcp_client):
    """Smithery trigger discovery should return an empty event catalog, not a validation error."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "ai.smithery/events/list", "id": 3}),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "SmitheryBot/1.0 (+https://smithery.ai)",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": 3, "result": {"events": []}}


@pytest.mark.asyncio
async def test_execution_blocked_for_all(mcp_client):
    """POST /tools/mcp requesting tools/call is blocked without auth."""
    resp = await mcp_client.post(
        "/tools/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "calculate"}, "id": 1}),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "SmitheryBot/1.0 (+https://smithery.ai)",
        },
    )
    # Execution is not bypassed, so it gets blocked by Phase 1 auth (401)
    assert resp.status_code == 401
