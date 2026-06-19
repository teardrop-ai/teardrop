# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Inbound A2A message endpoint.

Exposes a blocking ``POST /message:send`` endpoint that lets external agents
call the Teardrop agent over the A2A HTTP+JSON binding. Anonymous callers may
pay via x402; authenticated callers reuse the existing JWT + credit/x402
billing rails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, ValidationError, field_validator

from agent.state import AgentState
from billing import BillingResult, build_402_headers, build_402_response_body, get_byok_platform_fee
from billing.context import _get_pool
from marketplace import record_marketplace_tool_usage_many
from shared.audit import insert_event_row
from shared.request_ip import client_ip_from_request
from teardrop.a2a_client import A2AArtifact, A2AMessage, A2APart, A2ATask, A2ATaskStatus
from teardrop.agent_event_loop import _coerce_stream_text
from teardrop.agent_post_run import calculate_run_cost, dispatch_settlement, fetch_usage_snapshot
from teardrop.agent_runtime import _prepare_run_context, _record_marketplace_earnings, _run_billing_gate
from teardrop.auth import decode_access_token
from teardrop.config import get_settings
from teardrop.llm_config import get_org_llm_config_cached
from teardrop.rate_limit import _check_rate_limit, _enforce_rate_limit
from teardrop.usage import UsageEvent, record_usage_event

logger = logging.getLogger(__name__)
settings = get_settings()

_A2A_INBOUND_EVENT_INSERT_SQL = (
    "INSERT INTO a2a_inbound_events"
    " (id, run_id, usage_event_id, caller_org_id, caller_user_id, caller_address, caller_ip,"
    " auth_method, context_id, task_id, task_state, cost_usdc, settlement_tx, billing_method, duration_ms, error)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)"
)

router = APIRouter()


class A2ASendMessageRequest(BaseModel):
    message: A2AMessage
    metadata: dict[str, Any] = Field(default_factory=dict)
    context_id: str | None = Field(default=None, alias="contextId")
    task_id: str | None = Field(default=None, alias="taskId")

    model_config = {"populate_by_name": True, "extra": "allow"}

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v, default=str)) > 8_192:
            raise ValueError("A2A caller metadata exceeds 8 KB limit")
        return v


def _extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "").strip()
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def _parse_auth_payload(request: Request) -> dict[str, Any] | None:
    token = _extract_bearer_token(request)
    if token is None:
        return None
    try:
        return decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _request_client_ip(request: Request) -> str:
    return client_ip_from_request(request, trusted_proxy_count=settings.trusted_proxy_count)


async def _enforce_anonymous_rate_limit(request: Request) -> None:
    ip = _request_client_ip(request)
    if not ip:
        return
    allowed, remaining, reset_at = await _check_rate_limit(f"a2a:ip:{ip}", settings.rate_limit_requests_per_minute)
    if allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Anonymous rate limit exceeded",
        headers={
            "X-RateLimit-Limit": str(settings.rate_limit_requests_per_minute),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
            "Retry-After": "60",
        },
    )


def _parse_send_body(raw_body: Any) -> tuple[A2ASendMessageRequest, int | str | None]:
    rpc_id: int | str | None = None
    params = raw_body
    if isinstance(raw_body, dict) and "params" in raw_body:
        method = str(raw_body.get("method") or "").strip()
        if method and method not in {"message/send", "message:send"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported RPC method")
        rpc_id = raw_body.get("id")
        params = raw_body.get("params")
    if not isinstance(params, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Request body must be an object")
    try:
        return A2ASendMessageRequest.model_validate(params), rpc_id
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors())


def _message_text(message: A2AMessage) -> str:
    if message.role != "user":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A2A message role must be 'user'")
    parts: list[str] = []
    for part in message.parts:
        if part.kind == "text" and part.text:
            stripped = part.text.strip()
            if stripped:
                parts.append(stripped)
    if not parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A2A message must contain at least one non-empty text part",
        )
    combined = "\n".join(parts)
    if len(combined) > 4096:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A2A message text exceeds 4096 chars")
    return combined


def _snapshot_values(snapshot_or_state: Any) -> dict[str, Any]:
    if snapshot_or_state is None:
        return {}
    if isinstance(snapshot_or_state, dict):
        return snapshot_or_state
    if hasattr(snapshot_or_state, "values") and isinstance(snapshot_or_state.values, dict):
        return snapshot_or_state.values
    if hasattr(snapshot_or_state, "model_dump"):
        dumped = snapshot_or_state.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _extract_final_agent_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if getattr(message, "type", "") != "ai":
            continue
        text = _coerce_stream_text(getattr(message, "content", "")).strip()
        if text:
            return text
    for message in reversed(messages):
        text = _coerce_stream_text(getattr(message, "content", "")).strip()
        if text:
            return text
    return ""


def _anonymous_usage_identity(billing: BillingResult, run_id: str) -> tuple[str, str]:
    payer = getattr(billing.payment_payload, "payer", "") if billing.payment_payload is not None else ""
    payer_str = str(payer).strip()
    if payer_str:
        identity = f"x402:{payer_str}"
        return identity, identity
    return f"anonymous-a2a:{run_id}", "anonymous-a2a"


def _payment_caller_address(billing: BillingResult) -> str:
    payer = getattr(billing.payment_payload, "payer", "") if billing.payment_payload is not None else ""
    return str(payer).strip()


def _usage_identity(
    *,
    payload: dict[str, Any] | None,
    billing: BillingResult,
    user_id: str,
    org_id: str,
    run_id: str,
) -> tuple[str, str]:
    if payload is None:
        return _anonymous_usage_identity(billing, run_id)
    return user_id, org_id


def _response_error_text(response: JSONResponse) -> str:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "detail"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


async def _record_inbound_event(
    *,
    run_id: str,
    usage_event_id: str | None,
    caller_org_id: str,
    caller_user_id: str,
    caller_address: str,
    caller_ip: str,
    auth_method: str,
    context_id: str | None,
    task_id: str | None,
    task_state: str,
    cost_usdc: int,
    settlement_tx: str,
    billing_method: str,
    duration_ms: int,
    error: str,
) -> None:
    try:
        pool = _get_pool()
        await insert_event_row(
            pool,
            insert_sql=_A2A_INBOUND_EVENT_INSERT_SQL,
            values=(
                run_id,
                usage_event_id,
                caller_org_id,
                caller_user_id,
                caller_address,
                caller_ip,
                auth_method,
                context_id or "",
                task_id or "",
                task_state,
                cost_usdc,
                settlement_tx,
                billing_method,
                duration_ms,
                error[:1024],
            ),
        )
    except Exception:
        logger.warning("Failed to record inbound A2A audit event run_id=%s", run_id, exc_info=True)


async def _record_failure_usage_event(
    *,
    graph: Any,
    config: dict[str, Any],
    run_id: str,
    scoped_thread_id: str,
    payload: dict[str, Any] | None,
    billing: BillingResult,
    user_id: str,
    org_id: str,
    duration_ms: int,
    llm_config: dict[str, Any] | None,
    platform_fee: int,
) -> UsageEvent:
    _, usage_data = await fetch_usage_snapshot(
        graph=graph,
        config=config,
        run_id=run_id,
        settings=settings,
    )
    usage_user_id, usage_org_id = _usage_identity(
        payload=payload,
        billing=billing,
        user_id=user_id,
        org_id=org_id,
        run_id=run_id,
    )
    usage_event = UsageEvent(
        user_id=usage_user_id,
        org_id=usage_org_id,
        thread_id=scoped_thread_id,
        run_id=run_id,
        tokens_in=usage_data.get("tokens_in", 0),
        tokens_out=usage_data.get("tokens_out", 0),
        cache_read_tokens=usage_data.get("cache_read_tokens", 0),
        cache_creation_tokens=usage_data.get("cache_creation_tokens", 0),
        tool_calls=usage_data.get("tool_calls", 0),
        tool_names=usage_data.get("tool_names", []),
        billable_tool_calls=usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0)),
        billable_tool_names=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
        failed_tool_calls=usage_data.get("failed_tool_calls", 0),
        failed_tool_names=usage_data.get("failed_tool_names", []),
        duration_ms=duration_ms,
        cost_usdc=0,
        platform_fee_usdc=0 if platform_fee > 0 else 0,
        provider=llm_config["provider"] if llm_config else settings.agent_provider,
        model=llm_config["model"] if llm_config else settings.agent_model,
    )
    await record_usage_event(usage_event)
    return usage_event


def _build_task_response(
    *,
    request_body: A2ASendMessageRequest,
    task_id: str,
    task_state: str,
    output_text: str,
    rpc_id: int | str | None,
) -> JSONResponse:
    agent_message = A2AMessage(role="agent", parts=[A2APart(kind="text", text=output_text)])
    task = A2ATask(
        id=task_id,
        status=A2ATaskStatus(state=task_state, message=agent_message),
        artifacts=[A2AArtifact(name="result", parts=[A2APart(kind="text", text=output_text)])],
        history=[request_body.message, agent_message],
    )
    content: dict[str, Any] = task.model_dump(mode="json", by_alias=True)
    if rpc_id is not None:
        content = {"jsonrpc": "2.0", "id": rpc_id, "result": content}
    else:
        content = {"jsonrpc": "2.0", "result": content}
    return JSONResponse(content=content)


@router.post("/message:send", tags=["A2A"])
async def message_send(request: Request) -> JSONResponse:
    """Blocking inbound A2A endpoint for external agent callers."""
    if not settings.a2a_inbound_enabled:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "A2A inbound endpoint disabled"},
        )

    try:
        raw_body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid JSON body")

    body, rpc_id = _parse_send_body(raw_body)
    user_message = _message_text(body.message)
    payload = _parse_auth_payload(request)

    org_id = payload.get("org_id", "") if payload else ""
    user_id = payload.get("sub", "") if payload else ""
    auth_method = payload.get("auth_method", "") if payload else ""
    caller_ip = _request_client_ip(request)
    caller_org_id = org_id
    caller_user_id = user_id
    audit_auth_method = auth_method or "anonymous"

    if payload is None:
        await _enforce_anonymous_rate_limit(request)
    else:
        await _enforce_rate_limit(
            f"a2a:msg:user:{user_id}",
            settings.rate_limit_agent_rpm,
            detail="Rate limit exceeded. Please slow down.",
        )
        if org_id:
            await _enforce_rate_limit(
                f"a2a:msg:org:{org_id}",
                settings.rate_limit_org_agent_rpm,
                detail="Organization rate limit exceeded. Please slow down.",
                extra_headers={"X-RateLimit-Scope": "org"},
            )

    run_id = str(uuid.uuid4())
    resolved_task_id = body.task_id or run_id
    scoped_thread_id = f"{user_id or 'anonymous-a2a'}:{body.task_id or body.context_id or run_id}"

    org_llm_cfg = await get_org_llm_config_cached(org_id) if org_id else None
    is_byok = org_llm_cfg.is_byok if org_llm_cfg else False
    platform_fee = get_byok_platform_fee(is_byok)

    billing = BillingResult()
    if payload is None:
        if settings.billing_enabled:
            payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
            if not payment_header:
                try:
                    _402_body = build_402_response_body()
                    _402_hdrs = build_402_headers()
                except RuntimeError:
                    logger.warning("x402 billing not initialised; cannot issue payment requirements run_id=%s", run_id)
                    return JSONResponse(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        content={"error": "Payment service temporarily unavailable"},
                    )
                return JSONResponse(status_code=402, content=_402_body, headers=_402_hdrs)
            from billing import verify_payment

            billing = await verify_payment(payment_header)
            if not billing.verified:
                await _record_inbound_event(
                    run_id=run_id,
                    usage_event_id=None,
                    caller_org_id=caller_org_id,
                    caller_user_id=caller_user_id,
                    caller_address=_payment_caller_address(billing),
                    caller_ip=caller_ip,
                    auth_method=audit_auth_method,
                    context_id=body.context_id,
                    task_id=resolved_task_id,
                    task_state="rejected_payment",
                    cost_usdc=0,
                    settlement_tx="",
                    billing_method="x402",
                    duration_ms=0,
                    error=billing.error,
                )
                try:
                    _402_hdrs = build_402_headers()
                except RuntimeError:
                    _402_hdrs = {}
                return JSONResponse(
                    status_code=402,
                    content={"error": billing.error},
                    headers=_402_hdrs,
                )
    else:
        payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
        try:
            billing, gate_response = await _run_billing_gate(
                request,
                payload,
                org_id,
                is_byok=is_byok,
                platform_fee=platform_fee,
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_402_PAYMENT_REQUIRED:
                await _record_inbound_event(
                    run_id=run_id,
                    usage_event_id=None,
                    caller_org_id=caller_org_id,
                    caller_user_id=caller_user_id,
                    caller_address="",
                    caller_ip=caller_ip,
                    auth_method=audit_auth_method,
                    context_id=body.context_id,
                    task_id=resolved_task_id,
                    task_state="rejected_auth_credit",
                    cost_usdc=0,
                    settlement_tx="",
                    billing_method="credit",
                    duration_ms=0,
                    error=str(exc.detail),
                )
            raise
        if gate_response is not None:
            if payment_header:
                await _record_inbound_event(
                    run_id=run_id,
                    usage_event_id=None,
                    caller_org_id=caller_org_id,
                    caller_user_id=caller_user_id,
                    caller_address=_payment_caller_address(billing),
                    caller_ip=caller_ip,
                    auth_method=audit_auth_method,
                    context_id=body.context_id,
                    task_id=resolved_task_id,
                    task_state="rejected_payment",
                    cost_usdc=0,
                    settlement_tx="",
                    billing_method="x402",
                    duration_ms=0,
                    error=_response_error_text(gate_response),
                )
            return gate_response

    start_time = time.monotonic()
    mem_settings = get_settings()
    ctx = await _prepare_run_context(
        org_id=org_id,
        user_message=user_message,
        billing=billing,
        mem_settings=mem_settings,
    )
    graph = ctx.graph
    org_lc_tools = ctx.org_lc_tools
    org_tools_by_name = ctx.org_tools_by_name
    mp_by_name = ctx.mp_by_name
    recalled = ctx.recalled
    llm_config = ctx.llm_config
    org_name = ctx.org_name
    credit_balance_usdc = ctx.credit_balance_usdc

    initial_state = AgentState(
        messages=[HumanMessage(content=user_message)],
        metadata={
            **body.metadata,
            "thread_id": scoped_thread_id,
            "run_id": run_id,
            "user_id": user_id,
            "org_id": org_id,
            "_usage": {
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "tool_calls": 0,
                "tool_names": [],
                "billable_tool_calls": 0,
                "billable_tool_names": [],
                "failed_tool_calls": 0,
                "failed_tool_names": [],
            },
            "_excluded_tool_names": [],
            "_memories": recalled,
            "_llm_config": llm_config,
            "_org_name": org_name,
            "_user_role": payload.get("role", "anonymous") if payload else "anonymous",
            "_user_wallet_address": payload.get("address") if payload else None,
            "_credit_balance_usdc": credit_balance_usdc,
            "_jwt_token": _extract_bearer_token(request),
            "emit_ui": False,
            "a2a_context_id": body.context_id,
            "a2a_task_id": body.task_id,
            "a2a_auth_method": auth_method,
        },
    )
    config = {
        "configurable": {
            "thread_id": scoped_thread_id,
            "_org_tools": org_lc_tools,
            "_org_tools_by_name": org_tools_by_name,
        }
    }

    invoke_result: Any = None
    try:
        invoke_result = await asyncio.wait_for(
            graph.ainvoke(initial_state, config),
            timeout=float(settings.a2a_inbound_timeout_seconds),
        )
    except asyncio.TimeoutError:
        logger.warning("a2a inbound execution timed out run_id=%s", run_id)
        duration_ms = int((time.monotonic() - start_time) * 1000)
        usage_event = await _record_failure_usage_event(
            graph=graph,
            config=config,
            run_id=run_id,
            scoped_thread_id=scoped_thread_id,
            payload=payload,
            billing=billing,
            user_id=user_id,
            org_id=org_id,
            duration_ms=duration_ms,
            llm_config=llm_config,
            platform_fee=platform_fee,
        )
        await _record_inbound_event(
            run_id=run_id,
            usage_event_id=usage_event.id,
            caller_org_id=caller_org_id,
            caller_user_id=caller_user_id,
            caller_address=_payment_caller_address(billing),
            caller_ip=caller_ip,
            auth_method=audit_auth_method,
            context_id=body.context_id,
            task_id=resolved_task_id,
            task_state="timeout",
            cost_usdc=0,
            settlement_tx="",
            billing_method=billing.billing_method if billing.verified else "",
            duration_ms=duration_ms,
            error="Task timed out.",
        )
        return _build_task_response(
            request_body=body,
            task_id=resolved_task_id,
            task_state="failed",
            output_text="Task failed.",
            rpc_id=rpc_id,
        )
    except Exception:
        logger.exception("a2a inbound execution failed run_id=%s", run_id)
        duration_ms = int((time.monotonic() - start_time) * 1000)
        usage_event = await _record_failure_usage_event(
            graph=graph,
            config=config,
            run_id=run_id,
            scoped_thread_id=scoped_thread_id,
            payload=payload,
            billing=billing,
            user_id=user_id,
            org_id=org_id,
            duration_ms=duration_ms,
            llm_config=llm_config,
            platform_fee=platform_fee,
        )
        await _record_inbound_event(
            run_id=run_id,
            usage_event_id=usage_event.id,
            caller_org_id=caller_org_id,
            caller_user_id=caller_user_id,
            caller_address=_payment_caller_address(billing),
            caller_ip=caller_ip,
            auth_method=audit_auth_method,
            context_id=body.context_id,
            task_id=resolved_task_id,
            task_state="failed",
            cost_usdc=0,
            settlement_tx="",
            billing_method=billing.billing_method if billing.verified else "",
            duration_ms=duration_ms,
            error="Task failed.",
        )
        return _build_task_response(
            request_body=body,
            task_id=resolved_task_id,
            task_state="failed",
            output_text="Task failed.",
            rpc_id=rpc_id,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000)
    state_snapshot, usage_data = await fetch_usage_snapshot(
        graph=graph,
        config=config,
        run_id=run_id,
        settings=settings,
    )
    values = _snapshot_values(state_snapshot) or _snapshot_values(invoke_result)
    messages = values.get("messages", []) if isinstance(values, dict) else []
    output_text = _extract_final_agent_text(messages)
    task_status_value = str(values.get("task_status", "completed")).lower()
    task_state = "failed" if task_status_value.endswith("failed") else "completed"
    if not output_text:
        output_text = "Task failed." if task_state == "failed" else "Task completed."

    cost_usdc = await calculate_run_cost(
        usage_data=usage_data,
        llm_config=llm_config,
        settings=settings,
    )

    if payload is None:
        anon_user_id, anon_org_id = _anonymous_usage_identity(billing, run_id)
        user_id = anon_user_id
        org_id = anon_org_id

    usage_event = UsageEvent(
        user_id=user_id,
        org_id=org_id,
        thread_id=scoped_thread_id,
        run_id=run_id,
        tokens_in=usage_data.get("tokens_in", 0),
        tokens_out=usage_data.get("tokens_out", 0),
        cache_read_tokens=usage_data.get("cache_read_tokens", 0),
        cache_creation_tokens=usage_data.get("cache_creation_tokens", 0),
        tool_calls=usage_data.get("tool_calls", 0),
        tool_names=usage_data.get("tool_names", []),
        billable_tool_calls=usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0)),
        billable_tool_names=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
        failed_tool_calls=usage_data.get("failed_tool_calls", 0),
        failed_tool_names=usage_data.get("failed_tool_names", []),
        duration_ms=duration_ms,
        cost_usdc=cost_usdc,
        platform_fee_usdc=platform_fee,
        provider=llm_config["provider"] if llm_config else settings.agent_provider,
        model=llm_config["model"] if llm_config else settings.agent_model,
    )
    await record_usage_event(usage_event)

    settlement_result: dict[str, Any] = {}
    delegation_spend = usage_data.get("delegation_spend_usdc", 0)
    async for _ignored in dispatch_settlement(
        billing=billing,
        is_byok=is_byok,
        settings=settings,
        org_llm_cfg=org_llm_cfg,
        usage_data=usage_data,
        usage_event=usage_event,
        platform_fee=platform_fee,
        cost_usdc=cost_usdc,
        delegation_spend=delegation_spend,
        org_id=org_id,
        run_id=run_id,
        result=settlement_result,
    ):
        pass

    if settlement_result.get("marketplace_stats_billable", False):
        await _record_marketplace_earnings(
            mp_by_name=mp_by_name,
            tool_names_used=usage_data.get("billable_tool_names", usage_data.get("tool_names", [])),
            caller_org_id=org_id,
        )
        billable_tool_names = usage_data.get("billable_tool_names", usage_data.get("tool_names", []))
        if isinstance(billable_tool_names, list):
            asyncio.create_task(record_marketplace_tool_usage_many([str(name) for name in billable_tool_names]))

    await _record_inbound_event(
        run_id=run_id,
        usage_event_id=usage_event.id,
        caller_org_id=caller_org_id,
        caller_user_id=caller_user_id,
        caller_address=_payment_caller_address(billing),
        caller_ip=caller_ip,
        auth_method=audit_auth_method,
        context_id=body.context_id,
        task_id=resolved_task_id,
        task_state=task_state,
        cost_usdc=cost_usdc,
        settlement_tx=billing.tx_hash if billing.verified else "",
        billing_method=billing.billing_method if billing.verified else "",
        duration_ms=duration_ms,
        error=output_text if task_state == "failed" else "",
    )

    return _build_task_response(
        request_body=body,
        task_id=resolved_task_id,
        task_state=task_state,
        output_text=output_text,
        rpc_id=rpc_id,
    )
