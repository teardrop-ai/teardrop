"""API tests for POST /agent/run — billing gate and authentication.

Tests the billing gate logic that runs before the SSE stream begins:
  - Unauthenticated requests → 401
  - SIWE-authed requests without a payment header → 402
  - Credit-based auth with insufficient balance → 402
  - Successful request (billing disabled) → 200 SSE stream
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

# ─── Authentication ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_agent_run_requires_auth(anon_client):
    """Requests without a Bearer token should receive 401."""
    resp = await anon_client.post("/agent/run", json={"message": "hi"})
    assert resp.status_code == 401


# ─── Billing gate — SIWE (x402) ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_agent_run_siwe_no_payment_header_returns_402(test_settings, monkeypatch):
    """SIWE-authed call without X-Payment header must get 402 Payment Required."""
    from app import app
    from auth import require_auth

    async def _siwe_auth():
        return {
            "sub": "wallet-user-id",
            "email": "0xabc@wallet",
            "role": "user",
            "org_id": "wallet-org-id",
            "auth_method": "siwe",
        }

    mock_settings = MagicMock()
    mock_settings.billing_enabled = True
    mock_settings.billable_auth_methods = ["siwe", "email", "client_credentials"]
    mock_settings.rate_limit_requests_per_minute = 1_000
    mock_settings.rate_limit_agent_rpm = 1_000
    mock_settings.rate_limit_auth_rpm = 1_000
    mock_settings.app_env = "test"

    monkeypatch.setattr("app.settings", mock_settings)
    # SIWE with zero balance falls through to x402 path → no header → 402
    monkeypatch.setattr("app.get_credit_balance", AsyncMock(return_value=0))
    # Mock build_* so the 402 response body can be constructed without a
    # live billing server (_requirements_cache would be None otherwise)
    monkeypatch.setattr(
        "app.build_402_response_body",
        lambda: {"error": "Payment required", "accepts": []},
    )
    monkeypatch.setattr("app.build_402_headers", lambda: {})

    app.dependency_overrides[require_auth] = _siwe_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hello"})
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 402


# ─── Billing gate — credit ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_agent_run_insufficient_credit_returns_402(test_settings, monkeypatch):
    """Email-authed call with insufficient credit must get 402 Payment Required."""
    from app import app
    from auth import require_auth
    from billing import BillingResult

    async def _email_auth():
        return {
            "sub": "email-user-id",
            "email": "user@test.com",
            "role": "user",
            "org_id": "email-org-id",
            "auth_method": "email",
        }

    mock_settings = MagicMock()
    mock_settings.billing_enabled = True
    mock_settings.billable_auth_methods = ["siwe", "email", "client_credentials"]
    mock_settings.rate_limit_requests_per_minute = 1_000
    mock_settings.rate_limit_agent_rpm = 1_000
    mock_settings.rate_limit_auth_rpm = 1_000
    mock_settings.app_env = "test"

    monkeypatch.setattr("app.settings", mock_settings)
    monkeypatch.setattr(
        "app.get_current_pricing",
        AsyncMock(return_value=MagicMock(run_price_usdc=10_000)),
    )
    monkeypatch.setattr(
        "app.verify_credit",
        AsyncMock(
            return_value=BillingResult(
                verified=False,
                error="Insufficient credit: balance 0 atomic USDC, required 10000.",
            )
        ),
    )

    app.dependency_overrides[require_auth] = _email_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hello"})
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 402


# ─── Successful stream (billing disabled) ────────────────────────────────────


@pytest.mark.anyio
async def test_agent_run_returns_200_sse_when_billing_disabled(api_client, monkeypatch):
    """When billing is disabled a well-formed request returns 200 SSE stream."""

    async def _empty_astream_events(*args, **kwargs):
        return
        yield  # make this an async generator that yields nothing

    mock_graph = MagicMock()
    mock_graph.astream_events = _empty_astream_events
    mock_graph.aget_state = AsyncMock(
        return_value=MagicMock(
            values={"metadata": {"_usage": {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0, "tool_names": []}}}
        )
    )

    mock_settings = MagicMock()
    mock_settings.billing_enabled = False
    mock_settings.billable_auth_methods = []
    mock_settings.rate_limit_requests_per_minute = 1_000
    mock_settings.rate_limit_agent_rpm = 1_000
    mock_settings.rate_limit_auth_rpm = 1_000
    mock_settings.app_env = "test"
    mock_settings.agent_provider = "anthropic"
    mock_settings.agent_model = "claude-3-5-sonnet-20241022"

    monkeypatch.setattr("app.settings", mock_settings)
    monkeypatch.setattr("app.get_graph", AsyncMock(return_value=mock_graph))
    monkeypatch.setattr("app.build_org_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("app.build_mcp_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("app.record_usage_event", AsyncMock())
    monkeypatch.setattr("app.calculate_run_cost_usdc", AsyncMock(return_value=0))

    resp = await api_client.post("/agent/run", json={"message": "hello"})
    assert resp.status_code == 200


# ─── Thread scoping ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_agent_run_thread_id_scoped_to_user(api_client, monkeypatch):
    """Thread ID must be namespaced as '{user_id}:{thread_id}' to prevent hijacking."""
    captured: dict = {}

    async def _spy_astream_events(state_dict, config=None, version=None):
        captured["thread_id"] = config.get("configurable", {}).get("thread_id", "")
        return
        yield

    mock_graph = MagicMock()
    mock_graph.astream_events = _spy_astream_events
    mock_graph.aget_state = AsyncMock(return_value=MagicMock(values={}))

    mock_settings = MagicMock()
    mock_settings.billing_enabled = False
    mock_settings.billable_auth_methods = []
    mock_settings.rate_limit_requests_per_minute = 1_000
    mock_settings.rate_limit_agent_rpm = 1_000
    mock_settings.rate_limit_auth_rpm = 1_000
    mock_settings.app_env = "test"
    mock_settings.agent_provider = "anthropic"
    mock_settings.agent_model = "claude-3-5-sonnet-20241022"

    monkeypatch.setattr("app.settings", mock_settings)
    monkeypatch.setattr("app.get_graph", AsyncMock(return_value=mock_graph))
    monkeypatch.setattr("app.build_org_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("app.build_mcp_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("app.record_usage_event", AsyncMock())
    monkeypatch.setattr("app.calculate_run_cost_usdc", AsyncMock(return_value=0))

    await api_client.post("/agent/run", json={"message": "hi", "thread_id": "my-thread"})

    # Thread ID must start with the authenticated user's sub from api_client fixture
    assert captured.get("thread_id", "").startswith("test-user-id:")
    assert "my-thread" in captured.get("thread_id", "")
