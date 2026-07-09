"""API tests for inbound POST /message:send."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from billing import BillingResult


def _mock_ctx() -> SimpleNamespace:
    class _Graph:
        async def ainvoke(self, *_args, **_kwargs):
            return {
                "messages": [AIMessage(content="A2A result")],
                "task_status": "completed",
            }

    return SimpleNamespace(
        graph=_Graph(),
        org_lc_tools=[],
        org_tools_by_name={},
        mp_by_name={},
        recalled=[],
        llm_config=None,
        org_name="",
        credit_balance_usdc=None,
        persisted_excluded_tools=[],
    )


def _snapshot(text: str = "A2A result", task_status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        values={
            "messages": [AIMessage(content=text)],
            "task_status": task_status,
        }
    )


def _failing_ctx() -> SimpleNamespace:
    class _Graph:
        async def ainvoke(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    return SimpleNamespace(
        graph=_Graph(),
        org_lc_tools=[],
        org_tools_by_name={},
        mp_by_name={},
        recalled=[],
        llm_config=None,
        org_name="",
        credit_balance_usdc=None,
        persisted_excluded_tools=[],
    )


def _hanging_ctx() -> SimpleNamespace:
    class _Graph:
        async def ainvoke(self, *_args, **_kwargs):
            await asyncio.Future()

    return SimpleNamespace(
        graph=_Graph(),
        org_lc_tools=[],
        org_tools_by_name={},
        mp_by_name={},
        recalled=[],
        llm_config=None,
        org_name="",
        credit_balance_usdc=None,
        persisted_excluded_tools=[],
    )


async def _noop_dispatch_settlement(*_args, **kwargs):
    kwargs["result"]["marketplace_stats_billable"] = False
    if False:
        yield None


def _patch_success_path(monkeypatch, test_settings, *, billing_enabled: bool = True) -> None:
    test_settings.billing_enabled = billing_enabled
    test_settings.rate_limit_requests_per_minute = 1_000
    test_settings.rate_limit_agent_rpm = 1_000
    test_settings.rate_limit_org_agent_rpm = 1_000
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.agent_runtime._prepare_run_context", AsyncMock(return_value=_mock_ctx()))
    monkeypatch.setattr(
        "teardrop.agent_runtime.fetch_usage_snapshot",
        AsyncMock(return_value=(_snapshot(), {"tokens_in": 12, "tokens_out": 8, "tool_calls": 0, "tool_names": []})),
    )
    monkeypatch.setattr("teardrop.agent_runtime.calculate_run_cost", AsyncMock(return_value=12_345))
    monkeypatch.setattr("teardrop.agent_runtime.record_usage_event", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.agent_runtime.dispatch_settlement", _noop_dispatch_settlement)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", AsyncMock(return_value=None))


def test_a2a_bazaar_extension_uses_self_contained_body_schema():
    from x402.extensions.bazaar import validate_discovery_extension

    from teardrop.routers.a2a_messages import _a2a_402_extensions

    bazaar = _a2a_402_extensions()["bazaar"]
    body_schema = bazaar["schema"]["properties"]["input"]["properties"]["body"]

    result = validate_discovery_extension(bazaar)

    assert result.valid, result.errors
    assert '"$defs"' not in json.dumps(body_schema)
    assert '"$ref"' not in json.dumps(body_schema)
    assert body_schema["properties"]["message"]["type"] == "object"


@pytest.mark.anyio
async def test_message_send_anonymous_missing_payment_returns_402(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    audit_mock = AsyncMock(return_value=None)
    seen: dict[str, dict] = {}
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)

    def _body(**kwargs):
        seen["body"] = kwargs
        return {
            "error": kwargs["error"],
            "accepts": [],
            "x402Version": 2,
            "resource": kwargs["resource"],
            "extensions": kwargs["extensions"],
        }

    def _headers(**kwargs):
        seen["headers"] = kwargs
        return {"PAYMENT-REQUIRED": "abc", "X-PAYMENT-REQUIRED": "legacy"}

    monkeypatch.setattr("teardrop.routers.a2a_messages.build_402_response_body", _body)
    monkeypatch.setattr("teardrop.routers.a2a_messages.build_402_headers", _headers)

    resp = await anon_client.post(
        "/message:send",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 402
    assert resp.headers["payment-required"] == "abc"
    assert resp.headers["x-payment-required"] == "legacy"
    assert resp.json()["error"] == "Payment required"
    assert resp.json()["resource"]["url"] == "http://test/message:send"
    assert resp.json()["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"
    assert seen["body"]["resource"]["mimeType"] == "application/json"
    assert seen["headers"]["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"
    audit_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_message_send_anonymous_missing_payment_empty_body_returns_402(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    audit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.build_402_response_body",
        lambda **kwargs: {"error": "Payment required", "accepts": [], "x402Version": 2},
    )
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.build_402_headers",
        lambda **kwargs: {"PAYMENT-REQUIRED": "abc", "X-PAYMENT-REQUIRED": "abc"},
    )

    resp = await anon_client.post("/message:send")

    assert resp.status_code == 402
    assert resp.headers["payment-required"] == "abc"
    assert resp.headers["x-payment-required"] == "abc"
    assert resp.json()["error"] == "Payment required"
    audit_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_message_send_anonymous_missing_payment_invalid_json_returns_402(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    audit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.build_402_response_body",
        lambda **kwargs: {"error": "Payment required", "accepts": [], "x402Version": 2},
    )
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.build_402_headers",
        lambda **kwargs: {"PAYMENT-REQUIRED": "abc", "X-PAYMENT-REQUIRED": "abc"},
    )

    resp = await anon_client.post(
        "/message:send",
        content="{",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 402
    assert resp.headers["payment-required"] == "abc"
    assert resp.headers["x-payment-required"] == "abc"
    assert resp.json()["error"] == "Payment required"
    audit_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_message_send_anonymous_paid_invalid_json_returns_422(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)

    resp = await anon_client.post(
        "/message:send",
        content="{",
        headers={
            "Content-Type": "application/json",
            "X-PAYMENT": "signed-payment",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid JSON body"


@pytest.mark.anyio
async def test_message_send_authenticated_invalid_json_returns_422(auth_header, anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    test_settings.rate_limit_agent_rpm = 1_000
    test_settings.rate_limit_org_agent_rpm = 1_000
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)

    resp = await anon_client.post(
        "/message:send",
        content="{",
        headers={
            **auth_header,
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Invalid JSON body"


@pytest.mark.anyio
async def test_message_send_anonymous_x402_success_returns_task(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings)
    audit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr(
        "billing.verify_payment",
        AsyncMock(return_value=BillingResult(verified=True, payment_payload=SimpleNamespace(payer="0xabc"))),
    )

    resp = await anon_client.post(
        "/message:send",
        headers={"X-PAYMENT": "signed-payment"},
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["result"]["status"]["state"] == "completed"
    assert body["result"]["artifacts"][0]["parts"][0]["text"] == "A2A result"
    audit_mock.assert_awaited_once()
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["task_state"] == "completed"
    assert audit_kwargs["billing_method"] == "x402"
    assert audit_kwargs["caller_address"] == "0xabc"


@pytest.mark.anyio
async def test_message_send_authenticated_credit_success(auth_header, anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings)
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages._run_billing_gate",
        AsyncMock(return_value=(BillingResult(verified=True, billing_method="credit"), None)),
    )

    resp = await anon_client.post(
        "/message:send",
        headers=auth_header,
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["status"]["state"] == "completed"
    assert body["result"]["history"][0]["role"] == "user"
    assert body["result"]["history"][1]["role"] == "agent"


@pytest.mark.anyio
async def test_message_send_rejects_non_text_payload(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = False
    test_settings.rate_limit_requests_per_minute = 1_000
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)

    resp = await anon_client.post(
        "/message:send",
        json={"message": {"role": "user", "parts": [{"kind": "data", "data": {"query": "hi"}}]}},
    )

    assert resp.status_code == 422
    assert "text part" in resp.json()["detail"]


@pytest.mark.anyio
async def test_message_send_anonymous_invalid_payment_returns_402(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    audit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr(
        "billing.verify_payment",
        AsyncMock(return_value=BillingResult(verified=False, error="Payment verification failed: bad signature")),
    )
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.build_402_headers",
        lambda **kwargs: {"PAYMENT-REQUIRED": "1", "X-Payment-Required": "1"},
    )

    resp = await anon_client.post(
        "/message:send",
        headers={"X-PAYMENT": "bad-header"},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 402
    assert "Payment verification failed" in resp.json()["error"]
    audit_mock.assert_awaited_once()
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["task_state"] == "rejected_payment"
    assert audit_kwargs["billing_method"] == "x402"
    assert "bad signature" in audit_kwargs["error"]


@pytest.mark.anyio
async def test_message_send_authenticated_credit_gate_failure_records_audit(auth_header, anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = True
    test_settings.rate_limit_requests_per_minute = 1_000
    test_settings.rate_limit_agent_rpm = 1_000
    test_settings.rate_limit_org_agent_rpm = 1_000
    audit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages._run_billing_gate",
        AsyncMock(side_effect=HTTPException(status_code=402, detail="Insufficient credits")),
    )

    resp = await anon_client.post(
        "/message:send",
        headers=auth_header,
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 402
    audit_mock.assert_awaited_once()
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["task_state"] == "rejected_auth_credit"
    assert audit_kwargs["billing_method"] == "credit"
    assert audit_kwargs["error"] == "Insufficient credits"


@pytest.mark.anyio
async def test_message_send_execution_failure_records_audit(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = False
    test_settings.rate_limit_requests_per_minute = 1_000
    test_settings.rate_limit_agent_rpm = 1_000
    test_settings.rate_limit_org_agent_rpm = 1_000
    audit_mock = AsyncMock(return_value=None)
    usage_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.agent_runtime._prepare_run_context", AsyncMock(return_value=_failing_ctx()))
    monkeypatch.setattr(
        "teardrop.agent_runtime.fetch_usage_snapshot",
        AsyncMock(return_value=(_snapshot("Task failed.", "failed"), {"tokens_in": 9, "tokens_out": 3})),
    )
    monkeypatch.setattr("teardrop.agent_runtime.record_usage_event", usage_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)

    resp = await anon_client.post(
        "/message:send",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 200
    assert resp.json()["result"]["status"]["state"] == "failed"
    audit_mock.assert_awaited_once()
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["task_state"] == "failed"
    assert audit_kwargs["error"] == "Task failed."
    usage_mock.assert_awaited_once()
    usage_event = usage_mock.await_args.args[0]
    assert usage_event.cost_usdc == 0
    assert usage_event.tokens_in == 9
    assert usage_event.org_id == "anonymous-a2a"


@pytest.mark.anyio
async def test_message_send_timeout_records_zero_cost_usage(anon_client, test_settings, monkeypatch):
    test_settings.billing_enabled = False
    test_settings.rate_limit_requests_per_minute = 1_000
    test_settings.rate_limit_agent_rpm = 1_000
    test_settings.rate_limit_org_agent_rpm = 1_000
    test_settings.a2a_inbound_timeout_seconds = 0
    audit_mock = AsyncMock(return_value=None)
    usage_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_org_llm_config_cached", AsyncMock(return_value=None))
    monkeypatch.setattr("teardrop.agent_runtime._prepare_run_context", AsyncMock(return_value=_hanging_ctx()))
    monkeypatch.setattr(
        "teardrop.agent_runtime.fetch_usage_snapshot",
        AsyncMock(return_value=(_snapshot("Task failed.", "failed"), {"tokens_in": 4, "tokens_out": 2})),
    )
    monkeypatch.setattr("teardrop.agent_runtime.record_usage_event", usage_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)

    resp = await anon_client.post(
        "/message:send",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 200
    assert resp.json()["result"]["status"]["state"] == "failed"
    usage_mock.assert_awaited_once()
    usage_event = usage_mock.await_args.args[0]
    assert usage_event.cost_usdc == 0
    assert usage_event.tokens_in == 4
    audit_mock.assert_awaited_once()
    assert audit_mock.await_args.kwargs["task_state"] == "timeout"


@pytest.mark.anyio
async def test_message_send_returns_404_when_inbound_disabled(anon_client, test_settings, monkeypatch):
    test_settings.a2a_inbound_enabled = False
    monkeypatch.setattr("teardrop.routers.a2a_messages.settings", test_settings)

    resp = await anon_client.post(
        "/message:send",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 404
    assert resp.json()["error"] == "A2A inbound endpoint disabled"


@pytest.mark.anyio
async def test_message_send_billing_disabled_allows_anonymous(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)

    resp = await anon_client.post(
        "/message:send",
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 200
    assert resp.json()["result"]["status"]["state"] == "completed"
