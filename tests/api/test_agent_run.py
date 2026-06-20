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
    from teardrop.auth import require_auth
    from teardrop.main import app

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

    monkeypatch.setattr("teardrop.routers.agent.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.settings", mock_settings)
    # SIWE with zero balance falls through to x402 path → no header → 402
    monkeypatch.setattr("teardrop.agent_runtime.get_credit_balance", AsyncMock(return_value=0))
    # Mock build_* so the 402 response body can be constructed without a
    # live billing server (_requirements_cache would be None otherwise)
    monkeypatch.setattr(
        "teardrop.agent_runtime.build_402_response_body",
        lambda **kwargs: {
            "error": "Payment required",
            "accepts": [],
            "resource": kwargs["resource"],
            "x402Version": 2,
        },
    )
    monkeypatch.setattr(
        "teardrop.agent_runtime.build_402_headers",
        lambda **kwargs: {"PAYMENT-REQUIRED": "abc", "X-PAYMENT-REQUIRED": "legacy"},
    )

    app.dependency_overrides[require_auth] = _siwe_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/agent/run", json={"message": "hello"})
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 402
    assert resp.headers["payment-required"] == "abc"
    assert resp.json()["resource"]["url"] == "http://test/agent/run"


# ─── Billing gate — credit ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_agent_run_insufficient_credit_returns_402(test_settings, monkeypatch):
    """Email-authed call with insufficient credit must get 402 Payment Required."""
    from billing import BillingResult
    from teardrop.auth import require_auth
    from teardrop.main import app

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
    mock_settings.rate_limit_org_agent_rpm = 1_000
    mock_settings.credit_min_run_reserve_usdc = 0
    mock_settings.app_env = "test"

    monkeypatch.setattr("teardrop.routers.agent.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.settings", mock_settings)
    monkeypatch.setattr(
        "teardrop.agent_runtime.get_current_pricing",
        AsyncMock(return_value=MagicMock(run_price_usdc=10_000)),
    )
    monkeypatch.setattr(
        "teardrop.agent_runtime.verify_credit",
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

    monkeypatch.setattr("teardrop.routers.agent.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.get_graph", AsyncMock(return_value=mock_graph))
    monkeypatch.setattr("teardrop.agent_runtime.build_org_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.agent_runtime.build_mcp_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.routers.agent.record_usage_event", AsyncMock())
    monkeypatch.setattr("teardrop.agent_post_run.calculate_run_cost_usdc", AsyncMock(return_value=0))

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

    monkeypatch.setattr("teardrop.routers.agent.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.get_graph", AsyncMock(return_value=mock_graph))
    monkeypatch.setattr("teardrop.agent_runtime.build_org_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.agent_runtime.build_mcp_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.routers.agent.record_usage_event", AsyncMock())
    monkeypatch.setattr("teardrop.agent_post_run.calculate_run_cost_usdc", AsyncMock(return_value=0))

    await api_client.post("/agent/run", json={"message": "hi", "thread_id": "my-thread"})

    # Thread ID must start with the authenticated user's sub from api_client fixture
    assert captured.get("thread_id", "").startswith("test-user-id:")
    assert "my-thread" in captured.get("thread_id", "")


@pytest.mark.anyio
async def test_agent_run_tool_policy_normalizes_exclude_names(api_client, monkeypatch):
    """tool_policy.exclude_names should be normalized before entering graph state."""
    captured: dict = {}

    async def _spy_astream_events(state_dict, config=None, version=None):
        captured["excluded"] = state_dict.get("metadata", {}).get("_excluded_tool_names", [])
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

    monkeypatch.setattr("teardrop.routers.agent.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.get_graph", AsyncMock(return_value=mock_graph))
    monkeypatch.setattr("teardrop.agent_runtime.build_org_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.agent_runtime.build_mcp_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.routers.agent.record_usage_event", AsyncMock())
    monkeypatch.setattr("teardrop.agent_post_run.calculate_run_cost_usdc", AsyncMock(return_value=0))

    resp = await api_client.post(
        "/agent/run",
        json={
            "message": "hello",
            "tool_policy": {"exclude_names": ["platform/web_search", "org/my_tool", "acme/weather", "github__repos"]},
        },
    )

    assert resp.status_code == 200
    assert set(captured.get("excluded", [])) == {"web_search", "my_tool", "acme/weather", "github__repos"}


@pytest.mark.anyio
async def test_agent_run_tool_policy_rejects_too_many_exclusions(api_client):
    too_many = [f"platform/tool_{i}" for i in range(51)]
    resp = await api_client.post(
        "/agent/run",
        json={"message": "hello", "tool_policy": {"exclude_names": too_many}},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_agent_run_tool_policy_rejects_overlong_entry(api_client):
    too_long = "platform/" + ("a" * 201)
    resp = await api_client.post(
        "/agent/run",
        json={"message": "hello", "tool_policy": {"exclude_names": [too_long]}},
    )
    assert resp.status_code == 422


# ─── Marketplace earnings gated on confirmed settlement ──────────────────────


def _make_dispatch_settlement(*, billable: bool):
    """Return a stub ``dispatch_settlement`` async-gen that records the
    ``marketplace_stats_billable`` outcome without touching real billing."""

    async def _dispatch(**kwargs):
        kwargs["result"]["marketplace_stats_billable"] = billable
        return
        yield  # async generator that yields no SSE frames

    return _dispatch


def _earnings_harness(monkeypatch):
    """Wire up the minimal mocks needed to drive /agent/run to completion."""

    async def _empty_astream_events(*args, **kwargs):
        return
        yield

    mock_graph = MagicMock()
    mock_graph.astream_events = _empty_astream_events
    mock_graph.aget_state = AsyncMock(
        return_value=MagicMock(
            values={
                "metadata": {
                    "_usage": {
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "tool_calls": 1,
                        "tool_names": ["acme/weather"],
                        "billable_tool_names": ["acme/weather"],
                    }
                }
            }
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
    mock_settings.memory_enabled = False

    monkeypatch.setattr("teardrop.routers.agent.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.settings", mock_settings)
    monkeypatch.setattr("teardrop.agent_runtime.get_graph", AsyncMock(return_value=mock_graph))
    monkeypatch.setattr("teardrop.agent_runtime.build_org_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.agent_runtime.build_mcp_langchain_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("marketplace.build_subscribed_marketplace_tools", AsyncMock(return_value=([], {})))
    monkeypatch.setattr("teardrop.routers.agent.record_usage_event", AsyncMock())
    monkeypatch.setattr("teardrop.agent_post_run.calculate_run_cost_usdc", AsyncMock(return_value=0))


@pytest.mark.anyio
async def test_marketplace_earnings_skipped_when_not_billable(api_client, monkeypatch):
    """Earnings must NOT be recorded when settlement did not confirm payment —
    crediting tool authors for unpaid runs would let callers mint free earnings."""
    _earnings_harness(monkeypatch)
    earnings_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.agent._record_marketplace_earnings", earnings_mock)
    monkeypatch.setattr(
        "teardrop.routers.agent.dispatch_settlement",
        _make_dispatch_settlement(billable=False),
    )

    resp = await api_client.post("/agent/run", json={"message": "hello"})
    assert resp.status_code == 200
    # Consume the SSE stream so the generator runs to completion.
    async for _ in resp.aiter_lines():
        pass

    earnings_mock.assert_not_called()


@pytest.mark.anyio
async def test_marketplace_earnings_recorded_when_billable(api_client, monkeypatch):
    """Earnings ARE recorded once settlement confirms the caller paid."""
    _earnings_harness(monkeypatch)
    earnings_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.agent._record_marketplace_earnings", earnings_mock)
    monkeypatch.setattr(
        "teardrop.routers.agent.dispatch_settlement",
        _make_dispatch_settlement(billable=True),
    )

    resp = await api_client.post("/agent/run", json={"message": "hello"})
    assert resp.status_code == 200
    async for _ in resp.aiter_lines():
        pass

    earnings_mock.assert_awaited_once()
