# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

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


def _async_task(**overrides) -> SimpleNamespace:
    value = {
        "id": "internal-task-id",
        "run_id": "run-async",
        "client_task_id": "client-task-id",
        "context_id": "ctx-async",
        "message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]},
        "metadata": {},
        "user_message": "hello",
        "caller_org_id": "",
        "caller_user_id": "",
        "caller_ip": "198.51.100.7",
        "auth_method": "anonymous",
        "task_state": "submitted",
        "output_text": "",
        "error": "",
        "usage_event_id": None,
        "cost_usdc": 0,
        "settlement_tx": "",
        "billing_method": "",
        "duration_ms": 0,
    }
    value.update(overrides)
    return SimpleNamespace(**value)


async def _noop_dispatch_settlement(*_args, **kwargs):
    kwargs["result"]["marketplace_stats_billable"] = False
    if False:
        yield None


def _patch_success_path(monkeypatch, test_settings, *, billing_enabled: bool = True) -> AsyncMock:
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
    usage_event_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.agent_runtime.record_usage_event", usage_event_mock)
    monkeypatch.setattr("teardrop.agent_runtime.dispatch_settlement", _noop_dispatch_settlement)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", AsyncMock(return_value=None))
    return usage_event_mock


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
    usage_event_mock = _patch_success_path(monkeypatch, test_settings)
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
    assert usage_event_mock.await_args.args[0].source == "a2a"


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


@pytest.mark.anyio
async def test_message_send_async_returns_submitted_task_and_location(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    stored_task = _async_task()
    create_mock = AsyncMock(return_value=(stored_task, True))
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async"},
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]},
                "contextId": "ctx-async",
                "taskId": "client-task-id",
            },
        },
    )

    assert resp.status_code == 202
    assert resp.headers["location"] == "/message:status/internal-task-id"
    body = resp.json()
    assert body["id"] == 9
    assert body["result"]["id"] == "internal-task-id"
    assert body["result"]["status"]["state"] == "submitted"
    assert body["result"]["history"][0]["role"] == "user"
    assert body["result"]["history"][0]["parts"][0]["text"] == "hello"
    assert body["result"]["metadata"]["statusPath"] == "/message:status/internal-task-id"
    assert create_mock.await_args.kwargs["client_task_id"] == "client-task-id"
    assert enqueue_mock.await_args.args[0] == "internal-task-id"


@pytest.mark.anyio
async def test_message_send_async_queue_full_returns_retryable_503(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    stored_task = _async_task()
    finish_mock = AsyncMock(return_value=_async_task(task_state="failed", error="Task queue is full."))
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.create_inbound_task",
        AsyncMock(return_value=(stored_task, True)),
    )
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", AsyncMock(return_value=False))
    monkeypatch.setattr("teardrop.routers.a2a_messages.finish_inbound_task", finish_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", AsyncMock(return_value=None))

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async"},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert resp.json()["error"] == "A2A task queue is full"
    assert finish_mock.await_args.kwargs["task_state"] == "failed"


@pytest.mark.anyio
async def test_message_send_async_enqueue_error_returns_503(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    stored_task = _async_task()
    finish_mock = AsyncMock(return_value=_async_task(task_state="failed"))
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.create_inbound_task",
        AsyncMock(return_value=(stored_task, True)),
    )
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages.enqueue_inbound_task",
        AsyncMock(side_effect=RuntimeError("queue unavailable")),
    )
    monkeypatch.setattr("teardrop.routers.a2a_messages.finish_inbound_task", finish_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", AsyncMock(return_value=None))

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async"},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "A2A task queue temporarily unavailable"
    assert finish_mock.await_args.kwargs["task_state"] == "failed"


@pytest.mark.anyio
async def test_message_send_async_database_error_returns_503(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    create_mock = AsyncMock(side_effect=RuntimeError("database unavailable"))
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async"},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "A2A task queue temporarily unavailable"
    enqueue_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_async_worker_verifies_payment_only_after_claim(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=True)
    stored_task = _async_task()
    create_mock = AsyncMock(return_value=(stored_task, True))
    enqueue_mock = AsyncMock(return_value=True)
    claim_mock = AsyncMock(return_value=_async_task(task_state="running"))
    finish_mock = AsyncMock(return_value=_async_task(task_state="completed", output_text="A2A result"))
    audit_mock = AsyncMock(return_value=None)
    verify_mock = AsyncMock(return_value=BillingResult(verified=True, payment_payload=SimpleNamespace(payer="0xabc")))
    mark_billing_mock = AsyncMock()
    run_mock = AsyncMock(
        return_value=SimpleNamespace(
            task_state="completed",
            output_text="A2A result",
            duration_ms=12,
            usage_event=SimpleNamespace(id="usage-1", cost_usdc=123),
            settlement_amount_usdc=123,
            settlement_tx="0xsettled",
        )
    )
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.claim_inbound_task", claim_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.mark_inbound_task_billing_method", mark_billing_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.finish_inbound_task", finish_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr("billing.verify_payment", verify_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.run_agent_once", run_mock)

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async", "X-PAYMENT": "signed-payment"},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    assert resp.status_code == 202
    verify_mock.assert_not_awaited()
    runner = enqueue_mock.await_args.args[1]

    await runner()

    verify_mock.assert_awaited_once_with("signed-payment")
    run_mock.assert_awaited_once()
    assert finish_mock.await_args.kwargs["task_state"] == "completed"
    assert finish_mock.await_args.kwargs["usage_event_id"] == "usage-1"
    assert finish_mock.await_args.kwargs["settlement_amount_usdc"] == 123
    assert finish_mock.await_args.kwargs["settlement_tx"] == "0xsettled"
    mark_billing_mock.assert_awaited_once_with("internal-task-id", "x402")
    audit_mock.assert_awaited_once()
    assert audit_mock.await_args.kwargs["settlement_amount_usdc"] == 123
    assert audit_mock.await_args.kwargs["settlement_tx"] == "0xsettled"


@pytest.mark.anyio
async def test_async_worker_rejected_payment_records_x402_rail(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=True)
    stored_task = _async_task()
    create_mock = AsyncMock(return_value=(stored_task, True))
    enqueue_mock = AsyncMock(return_value=True)
    claim_mock = AsyncMock(return_value=_async_task(task_state="running"))
    finish_mock = AsyncMock(return_value=_async_task(task_state="rejected_payment", error="bad payment"))
    audit_mock = AsyncMock(return_value=None)
    verify_mock = AsyncMock(return_value=BillingResult(verified=False, error="bad payment"))
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.claim_inbound_task", claim_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.finish_inbound_task", finish_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr("billing.verify_payment", verify_mock)

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async", "X-PAYMENT": "bad-payment"},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    await enqueue_mock.await_args.args[1]()

    assert resp.status_code == 202
    assert finish_mock.await_args.kwargs["task_state"] == "rejected_payment"
    assert finish_mock.await_args.kwargs["billing_method"] == "x402"
    assert audit_mock.await_args.kwargs["billing_method"] == "x402"


@pytest.mark.anyio
async def test_async_worker_rejected_credit_records_credit_rail(auth_header, anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=True)
    stored_task = _async_task(caller_org_id="test-org-id", caller_user_id="test-user-id", auth_method="email")
    create_mock = AsyncMock(return_value=(stored_task, True))
    enqueue_mock = AsyncMock(return_value=True)
    claim_mock = AsyncMock(return_value=_async_task(task_state="running"))
    finish_mock = AsyncMock(return_value=_async_task(task_state="rejected_auth_credit", error="Insufficient credits"))
    audit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.claim_inbound_task", claim_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.finish_inbound_task", finish_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)
    monkeypatch.setattr(
        "teardrop.routers.a2a_messages._run_billing_gate",
        AsyncMock(side_effect=HTTPException(status_code=402, detail="Insufficient credits")),
    )

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async", **auth_header},
        json={"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
    )

    await enqueue_mock.await_args.args[1]()

    assert resp.status_code == 202
    assert finish_mock.await_args.kwargs["task_state"] == "rejected_auth_credit"
    assert finish_mock.await_args.kwargs["billing_method"] == "credit"
    assert audit_mock.await_args.kwargs["billing_method"] == "credit"


@pytest.mark.anyio
async def test_message_send_async_retry_reuses_existing_task(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=True)
    stored_task = _async_task(task_state="running")
    create_mock = AsyncMock(return_value=(stored_task, False))
    enqueue_mock = AsyncMock(return_value=True)
    verify_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)
    monkeypatch.setattr("billing.verify_payment", verify_mock)

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async", "X-PAYMENT": "same-payment"},
        json={
            "message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]},
            "contextId": "ctx-async",
            "taskId": "client-task-id",
        },
    )

    assert resp.status_code == 202
    assert resp.json()["result"]["id"] == "internal-task-id"
    assert resp.json()["result"]["status"]["state"] == "working"
    enqueue_mock.assert_not_awaited()
    verify_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_message_send_async_retry_rejects_mismatched_request(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    create_mock = AsyncMock(return_value=(_async_task(), False))
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("teardrop.routers.a2a_messages.create_inbound_task", create_mock)
    monkeypatch.setattr("teardrop.routers.a2a_messages.enqueue_inbound_task", enqueue_mock)

    resp = await anon_client.post(
        "/message:send",
        headers={"Prefer": "respond-async"},
        json={
            "message": {"role": "user", "parts": [{"kind": "text", "text": "different"}]},
            "taskId": "client-task-id",
        },
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Client task ID is already associated with a different request"
    enqueue_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_message_status_scopes_authenticated_lookup(auth_header, anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    task = _async_task(
        caller_org_id="test-org-id",
        caller_user_id="test-user-id",
        auth_method="email",
        task_state="completed",
        output_text="A2A result",
    )
    lookup_mock = AsyncMock(return_value=task)
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_inbound_task", lookup_mock)

    resp = await anon_client.get("/message:status/internal-task-id", headers=auth_header)

    assert resp.status_code == 200
    assert resp.json()["result"]["status"]["state"] == "completed"
    assert resp.json()["result"]["artifacts"][0]["parts"][0]["text"] == "A2A result"
    lookup_mock.assert_awaited_once_with(
        "internal-task-id",
        caller_org_id="test-org-id",
        caller_user_id="test-user-id",
    )


@pytest.mark.anyio
async def test_message_status_uses_anonymous_task_id_as_capability(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    lookup_mock = AsyncMock(return_value=_async_task(task_state="completed", output_text="A2A result"))
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_inbound_task", lookup_mock)

    resp = await anon_client.get("/message:status/internal-task-id")

    assert resp.status_code == 200
    assert resp.json()["result"]["status"]["state"] == "completed"
    lookup_mock.assert_awaited_once_with("internal-task-id", anonymous_only=True)


@pytest.mark.anyio
async def test_message_status_returns_404_for_unknown_task(anon_client, test_settings, monkeypatch):
    _patch_success_path(monkeypatch, test_settings, billing_enabled=False)
    lookup_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("teardrop.routers.a2a_messages.get_inbound_task", lookup_mock)

    resp = await anon_client.get("/message:status/missing-task")

    assert resp.status_code == 404
    assert resp.json()["error"] == "A2A task not found"
