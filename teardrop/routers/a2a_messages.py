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
import uuid
from copy import deepcopy
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

from billing import BillingResult, build_402_headers, build_402_response_body, get_byok_platform_fee
from billing.context import _get_pool
from shared.audit import insert_event_row
from shared.request_ip import client_ip_from_request
from teardrop.a2a_client import A2AArtifact, A2AMessage, A2APart, A2ATask, A2ATaskStatus
from teardrop.a2a_tasks import (
    TERMINAL_INBOUND_TASK_STATES,
    A2AInboundTask,
    claim_inbound_task,
    create_inbound_task,
    enqueue_inbound_task,
    finish_inbound_task,
    get_inbound_task,
    mark_inbound_task_billing_method,
)
from teardrop.agent_runtime import _run_billing_gate, run_agent_once
from teardrop.auth import decode_access_token
from teardrop.config import get_settings
from teardrop.llm_config import get_org_llm_config_cached
from teardrop.public_url import public_base_url
from teardrop.rate_limit import _check_rate_limit, _enforce_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

_A2A_INBOUND_EVENT_INSERT_SQL = (
    "INSERT INTO a2a_inbound_events"
    " (id, run_id, usage_event_id, caller_org_id, caller_user_id, caller_address, caller_ip,"
    " auth_method, context_id, task_id, task_state, cost_usdc, settlement_amount_usdc,"
    " settlement_tx, billing_method, duration_ms, error)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)"
)

router = APIRouter()


class A2ASendMessageRequest(BaseModel):
    message: A2AMessage
    metadata: dict[str, Any] = Field(default_factory=dict)
    context_id: str | None = Field(default=None, alias="contextId", max_length=256)
    task_id: str | None = Field(default=None, alias="taskId", max_length=256)

    model_config = {"populate_by_name": True, "extra": "allow"}

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_size(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v, default=str)) > 8_192:
            raise ValueError("A2A caller metadata exceeds 8 KB limit")
        return v


_A2A_BAZAAR_INPUT_EXAMPLE = {
    "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "Summarize the top 3 takeaways from the last run."}],
    },
    "metadata": {"source": "agentic.market"},
    "contextId": "ctx_demo",
    "taskId": "task_demo",
}

_A2A_BAZAAR_OUTPUT_EXAMPLE = {
    "jsonrpc": "2.0",
    "result": {
        "id": "task_demo",
        "status": {
            "state": "completed",
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": "Here are the top takeaways..."}],
            },
        },
        "artifacts": [
            {
                "name": "result",
                "parts": [{"kind": "text", "text": "Here are the top takeaways..."}],
            }
        ],
        "history": [
            _A2A_BAZAAR_INPUT_EXAMPLE["message"],
            {
                "role": "agent",
                "parts": [{"kind": "text", "text": "Here are the top takeaways..."}],
            },
        ],
    },
}


def _flatten_embedded_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local refs so an embedded schema remains self-contained.

    Pydantic emits ``$defs`` plus root-anchored ``$ref`` values. Once that
    schema is nested under Bazaar's ``info.input.body``, those references no
    longer point at valid locations. Inline them before embedding so the body
    schema validates both locally and with external discovery validators.
    """

    root = deepcopy(schema)
    defs: dict[str, Any] = {}
    for defs_key in ("$defs", "definitions"):
        defs_value = root.pop(defs_key, None)
        if isinstance(defs_value, dict):
            defs.update(defs_value)

    def _resolve(node: Any, seen: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [_resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node

        ref = node.get("$ref")
        if isinstance(ref, str):
            ref_name = ""
            if ref.startswith("#/$defs/"):
                ref_name = ref.rsplit("/", 1)[-1]
            elif ref.startswith("#/definitions/"):
                ref_name = ref.rsplit("/", 1)[-1]
            if ref_name and ref_name in defs:
                if ref_name in seen:
                    raise ValueError(f"Recursive schema reference: {ref_name}")
                resolved = _resolve(deepcopy(defs[ref_name]), seen + (ref_name,))
                if isinstance(resolved, dict):
                    for key, value in node.items():
                        if key == "$ref":
                            continue
                        resolved[key] = _resolve(value, seen)
                return resolved

        flattened: dict[str, Any] = {}
        for key, value in node.items():
            if key in {"$defs", "definitions"}:
                continue
            flattened[key] = _resolve(value, seen)
        return flattened

    return _resolve(root)


def _a2a_402_resource(request: Request) -> dict[str, str]:
    base_url = public_base_url(request, settings)
    return {
        "url": f"{base_url}/message:send",
        "description": "Public A2A endpoint for external agent callers.",
        "mimeType": "application/json",
    }


def _a2a_402_extensions() -> dict[str, Any]:
    extension = declare_discovery_extension(
        input=_A2A_BAZAAR_INPUT_EXAMPLE,
        input_schema=_flatten_embedded_json_schema(A2ASendMessageRequest.model_json_schema(by_alias=True)),
        body_type="json",
        output=OutputConfig(example=_A2A_BAZAAR_OUTPUT_EXAMPLE),
    )
    bazaar = extension.get("bazaar")
    if isinstance(bazaar, dict):
        info = bazaar.setdefault("info", {})
        input_data = info.setdefault("input", {})
        input_data["method"] = "POST"
    return extension


def _a2a_402_kwargs(request: Request, *, error: str | None = "Payment required") -> dict[str, Any]:
    return {
        "error": error,
        "resource": _a2a_402_resource(request),
        "extensions": _a2a_402_extensions(),
    }


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


def _prefers_async(request: Request) -> bool:
    """Return whether the caller explicitly requested asynchronous execution."""
    if not settings.a2a_inbound_async_enabled:
        return False
    return any(part.strip().split(";", 1)[0].lower() == "respond-async" for part in request.headers.get("prefer", "").split(","))


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
    settlement_amount_usdc: int = 0,
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
                max(0, settlement_amount_usdc),
                settlement_tx,
                billing_method,
                duration_ms,
                error[:1024],
            ),
        )
    except Exception:
        logger.warning("Failed to record inbound A2A audit event run_id=%s", run_id, exc_info=True)


def _build_task_response(
    *,
    request_body: A2ASendMessageRequest,
    task_id: str,
    task_state: str,
    output_text: str,
    rpc_id: int | str | None,
    status_path: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    agent_message = A2AMessage(role="agent", parts=[A2APart(kind="text", text=output_text)]) if output_text else None
    task = A2ATask(
        id=task_id,
        status=A2ATaskStatus(state=task_state, message=agent_message),
        artifacts=([A2AArtifact(name="result", parts=[A2APart(kind="text", text=output_text)])] if output_text else []),
        history=[request_body.message, *([agent_message] if agent_message is not None else [])],
    )
    content: dict[str, Any] = task.model_dump(mode="json", by_alias=True)
    if status_path:
        content["metadata"] = {"statusPath": status_path}
    if rpc_id is not None:
        content = {"jsonrpc": "2.0", "id": rpc_id, "result": content}
    else:
        content = {"jsonrpc": "2.0", "result": content}
    headers = {"Location": status_path} if status_path else None
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _task_response_state(task_state: str) -> str:
    return {
        "submitted": "submitted",
        "running": "working",
        "completed": "completed",
        "failed": "failed",
        "timeout": "failed",
        "rejected_payment": "failed",
        "rejected_auth_credit": "failed",
    }.get(task_state, "failed")


def _task_request_body(task: A2AInboundTask) -> A2ASendMessageRequest:
    return A2ASendMessageRequest(
        message=A2AMessage.model_validate(task.message),
        metadata=task.metadata,
        contextId=task.context_id or None,
        taskId=task.client_task_id or None,
    )


def _task_request_matches(task: A2AInboundTask, request_body: A2ASendMessageRequest) -> bool:
    """Keep a client task ID idempotent without accepting a different request."""
    try:
        stored_message = A2AMessage.model_validate(task.message).model_dump(mode="json", by_alias=True, exclude_none=True)
    except ValidationError:
        return False
    incoming_message = request_body.message.model_dump(mode="json", by_alias=True, exclude_none=True)
    return (
        stored_message == incoming_message
        and task.metadata == request_body.metadata
        and task.context_id == (request_body.context_id or "")
        and task.client_task_id == (request_body.task_id or "")
    )


def _task_status_path(task_id: str) -> str:
    return f"/message:status/{task_id}"


async def _finish_async_task(
    *,
    task: A2AInboundTask,
    task_state: str,
    output_text: str = "",
    error: str = "",
    billing: BillingResult | None = None,
    usage_event_id: str | None = None,
    cost_usdc: int = 0,
    settlement_amount_usdc: int | None = None,
    settlement_tx: str | None = None,
    duration_ms: int = 0,
) -> None:
    """Persist a terminal projection and append the corresponding audit row."""
    billing_result = billing or BillingResult()
    billing_method = (
        billing_result.billing_method
        if billing_result.verified
        else {"rejected_payment": "x402", "rejected_auth_credit": "credit"}.get(task_state, "")
    )
    persisted_settlement_tx = (
        settlement_tx if settlement_tx is not None else billing_result.tx_hash if billing_result.verified else ""
    )
    persisted_settlement_amount = (
        max(0, settlement_amount_usdc)
        if settlement_amount_usdc is not None
        else max(0, billing_result.amount_usdc if billing_result.verified else 0)
    )
    finished = await finish_inbound_task(
        task.id,
        task_state=task_state,
        output_text=output_text,
        error=error,
        usage_event_id=usage_event_id,
        cost_usdc=cost_usdc,
        settlement_tx=persisted_settlement_tx,
        billing_method=billing_method,
        settlement_amount_usdc=persisted_settlement_amount,
        duration_ms=duration_ms,
    )
    if finished is None:
        return
    await _record_inbound_event(
        run_id=task.run_id,
        usage_event_id=usage_event_id,
        caller_org_id=task.caller_org_id,
        caller_user_id=task.caller_user_id,
        caller_address=_payment_caller_address(billing_result),
        caller_ip=task.caller_ip,
        auth_method=task.auth_method,
        context_id=task.context_id,
        task_id=task.id,
        task_state=task_state,
        cost_usdc=max(0, cost_usdc),
        settlement_amount_usdc=persisted_settlement_amount,
        settlement_tx=persisted_settlement_tx,
        billing_method=billing_method,
        duration_ms=max(0, duration_ms),
        error=error,
    )


async def _run_async_inbound_task(
    *,
    request: Request,
    task: A2AInboundTask,
    request_body: A2ASendMessageRequest,
    payload: dict[str, Any] | None,
    payment_header: str | None,
    jwt_token: str | None,
) -> None:
    claimed = await claim_inbound_task(task.id)
    if claimed is None:
        return

    billing = BillingResult()
    try:
        org_id = task.caller_org_id
        user_id = task.caller_user_id
        org_llm_cfg = await get_org_llm_config_cached(org_id) if org_id else None
        is_byok = org_llm_cfg.is_byok if org_llm_cfg else False
        platform_fee = get_byok_platform_fee(is_byok)

        if payload is None:
            if settings.billing_enabled:
                from billing import verify_payment

                billing = await verify_payment(payment_header)
                if not billing.verified:
                    await _finish_async_task(
                        task=claimed,
                        task_state="rejected_payment",
                        output_text=billing.error,
                        error=billing.error,
                        billing=billing,
                    )
                    return
        else:
            try:
                billing, gate_response = await _run_billing_gate(
                    request,
                    payload,
                    org_id,
                    is_byok=is_byok,
                    platform_fee=platform_fee,
                )
            except HTTPException as exc:
                if exc.status_code != status.HTTP_402_PAYMENT_REQUIRED:
                    raise
                error = str(exc.detail)
                await _finish_async_task(
                    task=claimed,
                    task_state="rejected_auth_credit",
                    output_text=error,
                    error=error,
                    billing=BillingResult(billing_method="credit"),
                )
                return
            if gate_response is not None:
                error = _response_error_text(gate_response) or "Payment required"
                await _finish_async_task(
                    task=claimed,
                    task_state="rejected_payment",
                    output_text=error,
                    error=error,
                    billing=billing,
                )
                return

        if billing.verified:
            await mark_inbound_task_billing_method(task.id, billing.billing_method)

        run_id = task.run_id
        usage_user_id, usage_org_id = _usage_identity(
            payload=payload,
            billing=billing,
            user_id=user_id,
            org_id=org_id,
            run_id=run_id,
        )
        result = await run_agent_once(
            org_id=org_id,
            user_id=user_id,
            usage_user_id=usage_user_id,
            usage_org_id=usage_org_id,
            user_message=task.user_message,
            run_id=run_id,
            thread_id=f"{user_id or 'anonymous-a2a'}:{task.client_task_id or task.context_id or run_id}",
            billing=billing,
            is_byok=is_byok,
            org_llm_cfg=org_llm_cfg,
            platform_fee=platform_fee,
            timeout_seconds=float(settings.a2a_inbound_timeout_seconds),
            source="a2a",
            metadata={
                **request_body.metadata,
                "a2a_context_id": request_body.context_id,
                "a2a_task_id": request_body.task_id,
                "a2a_auth_method": payload.get("auth_method", "") if payload else "",
            },
            user_role=payload.get("role", "anonymous") if payload else "anonymous",
            user_wallet_address=payload.get("address") if payload else None,
            jwt_token=jwt_token,
            emit_ui=False,
        )
        output_text = "Task failed." if result.task_state == "timeout" else result.output_text
        error = result.output_text if result.task_state != "completed" else ""
        await _finish_async_task(
            task=claimed,
            task_state=result.task_state,
            output_text=output_text,
            error=error,
            billing=billing,
            usage_event_id=result.usage_event.id,
            cost_usdc=result.usage_event.cost_usdc,
            settlement_amount_usdc=max(0, int(getattr(result, "settlement_amount_usdc", 0) or 0)),
            settlement_tx=str(getattr(result, "settlement_tx", "") or ""),
            duration_ms=result.duration_ms,
        )
    except asyncio.CancelledError:
        await _finish_async_task(
            task=claimed,
            task_state="failed",
            output_text="Task cancelled before completion.",
            error="Task cancelled before completion.",
            billing=billing,
        )
        raise
    except Exception:
        logger.exception("Inbound A2A task failed task_id=%s", task.id)
        await _finish_async_task(
            task=claimed,
            task_state="failed",
            output_text="Task failed.",
            error="Task failed.",
            billing=billing,
        )


@router.get("/message:status/{task_id}", tags=["A2A"])
async def message_status(task_id: str, request: Request) -> JSONResponse:
    """Return the status and terminal result for an asynchronous inbound task."""
    if not settings.a2a_inbound_enabled:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "A2A inbound endpoint disabled"},
        )

    payload = _parse_auth_payload(request)
    if payload is None:
        task = await get_inbound_task(task_id, anonymous_only=True)
    else:
        org_id = payload.get("org_id", "")
        user_id = payload.get("sub", "")
        task = await get_inbound_task(task_id, caller_org_id=org_id, caller_user_id=user_id) if org_id and user_id else None
    if task is None:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "A2A task not found"})

    output_text = task.output_text or task.error
    return _build_task_response(
        request_body=_task_request_body(task),
        task_id=task.id,
        task_state=_task_response_state(task.task_state),
        output_text=output_text,
        rpc_id=None,
        status_path=(_task_status_path(task.id) if task.task_state not in TERMINAL_INBOUND_TASK_STATES else None),
    )


@router.post("/message:send", tags=["A2A"])
async def message_send(request: Request) -> JSONResponse:
    """Inbound A2A endpoint with opt-in asynchronous execution."""
    if not settings.a2a_inbound_enabled:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "A2A inbound endpoint disabled"},
        )

    run_id = str(uuid.uuid4())
    payload = _parse_auth_payload(request)
    payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")

    org_id = payload.get("org_id", "") if payload else ""
    user_id = payload.get("sub", "") if payload else ""
    auth_method = payload.get("auth_method", "") if payload else ""
    caller_ip = _request_client_ip(request)
    caller_org_id = org_id
    caller_user_id = user_id
    audit_auth_method = auth_method or "anonymous"

    if payload is None:
        await _enforce_anonymous_rate_limit(request)
        if settings.billing_enabled and not payment_header:
            # Registry probes may omit or malform the body; unpaid anonymous
            # callers should still receive the x402 challenge first.
            response_kwargs = _a2a_402_kwargs(request)
            try:
                _402_body = build_402_response_body(**response_kwargs)
                _402_hdrs = build_402_headers(**response_kwargs)
            except RuntimeError:
                logger.warning("x402 billing not initialised; cannot issue payment requirements run_id=%s", run_id)
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"error": "Payment service temporarily unavailable"},
                )
            return JSONResponse(status_code=402, content=_402_body, headers=_402_hdrs)
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

    try:
        raw_body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid JSON body")

    body, rpc_id = _parse_send_body(raw_body)
    user_message = _message_text(body.message)
    resolved_task_id = body.task_id or run_id
    scoped_thread_id = f"{user_id or 'anonymous-a2a'}:{body.task_id or body.context_id or run_id}"

    if _prefers_async(request):
        async_task_id = str(uuid.uuid4())
        task: A2AInboundTask | None = None
        try:
            task, created = await create_inbound_task(
                task_id=async_task_id,
                run_id=run_id,
                client_task_id=body.task_id,
                context_id=body.context_id,
                message=body.message.model_dump(mode="json", by_alias=True),
                metadata=body.metadata,
                user_message=user_message,
                caller_org_id=caller_org_id,
                caller_user_id=caller_user_id,
                caller_ip=caller_ip,
                auth_method=audit_auth_method,
            )
        except Exception:
            if task is not None:
                try:
                    await _finish_async_task(
                        task=task,
                        task_state="failed",
                        output_text="Task queue temporarily unavailable.",
                        error="Task queue temporarily unavailable.",
                    )
                except Exception:
                    logger.warning("Unable to finalize unqueued inbound A2A task task_id=%s", task.id, exc_info=True)
            logger.warning("Unable to enqueue inbound A2A task task_id=%s", async_task_id, exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "A2A task queue temporarily unavailable"},
            )
        if not created:
            if not _task_request_matches(task, body):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Client task ID is already associated with a different request",
                )
            existing_output = task.output_text or task.error
            return _build_task_response(
                request_body=body,
                task_id=task.id,
                task_state=_task_response_state(task.task_state),
                output_text=existing_output,
                rpc_id=rpc_id,
                status_path=(_task_status_path(task.id) if task.task_state not in TERMINAL_INBOUND_TASK_STATES else None),
                status_code=(
                    status.HTTP_202_ACCEPTED if task.task_state not in TERMINAL_INBOUND_TASK_STATES else status.HTTP_200_OK
                ),
            )
        try:
            queued = await enqueue_inbound_task(
                task.id,
                lambda: _run_async_inbound_task(
                    request=request,
                    task=task,
                    request_body=body,
                    payload=payload,
                    payment_header=payment_header,
                    jwt_token=_extract_bearer_token(request),
                ),
            )
        except Exception:
            try:
                await _finish_async_task(
                    task=task,
                    task_state="failed",
                    output_text="Task queue temporarily unavailable.",
                    error="Task queue temporarily unavailable.",
                )
            except Exception:
                logger.warning("Unable to finalize unqueued inbound A2A task task_id=%s", task.id, exc_info=True)
            logger.warning("Unable to enqueue inbound A2A task task_id=%s", task.id, exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "A2A task queue temporarily unavailable"},
            )
        if not queued:
            try:
                await _finish_async_task(
                    task=task,
                    task_state="failed",
                    output_text="Task queue is full.",
                    error="Task queue is full.",
                )
            except Exception:
                logger.warning("Unable to finalize full inbound A2A task task_id=%s", task.id, exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "A2A task queue is full"},
                headers={"Retry-After": "1"},
            )
        return _build_task_response(
            request_body=body,
            task_id=task.id,
            task_state="submitted",
            output_text="",
            rpc_id=rpc_id,
            status_path=_task_status_path(task.id),
            status_code=status.HTTP_202_ACCEPTED,
        )

    org_llm_cfg = await get_org_llm_config_cached(org_id) if org_id else None
    is_byok = org_llm_cfg.is_byok if org_llm_cfg else False
    platform_fee = get_byok_platform_fee(is_byok)

    billing = BillingResult()
    if payload is None:
        if settings.billing_enabled:
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
                    settlement_amount_usdc=0,
                    settlement_tx="",
                    billing_method="x402",
                    duration_ms=0,
                    error=billing.error,
                )
                response_kwargs = _a2a_402_kwargs(request, error=billing.error)
                try:
                    _402_body = build_402_response_body(**response_kwargs)
                    _402_hdrs = build_402_headers(**response_kwargs)
                except RuntimeError:
                    _402_body = {"error": billing.error}
                    _402_hdrs = {}
                return JSONResponse(
                    status_code=402,
                    content=_402_body,
                    headers=_402_hdrs,
                )
    else:
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
                    settlement_amount_usdc=0,
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

    usage_user_id, usage_org_id = _usage_identity(
        payload=payload,
        billing=billing,
        user_id=user_id,
        org_id=org_id,
        run_id=run_id,
    )
    result = await run_agent_once(
        org_id=org_id,
        user_id=user_id,
        usage_user_id=usage_user_id,
        usage_org_id=usage_org_id,
        user_message=user_message,
        run_id=run_id,
        thread_id=scoped_thread_id,
        billing=billing,
        is_byok=is_byok,
        org_llm_cfg=org_llm_cfg,
        platform_fee=platform_fee,
        timeout_seconds=float(settings.a2a_inbound_timeout_seconds),
        source="a2a",
        metadata={
            **body.metadata,
            "a2a_context_id": body.context_id,
            "a2a_task_id": body.task_id,
            "a2a_auth_method": auth_method,
        },
        user_role=payload.get("role", "anonymous") if payload else "anonymous",
        user_wallet_address=payload.get("address") if payload else None,
        jwt_token=_extract_bearer_token(request),
        emit_ui=False,
    )

    await _record_inbound_event(
        run_id=run_id,
        usage_event_id=result.usage_event.id,
        caller_org_id=caller_org_id,
        caller_user_id=caller_user_id,
        caller_address=_payment_caller_address(billing),
        caller_ip=caller_ip,
        auth_method=audit_auth_method,
        context_id=body.context_id,
        task_id=resolved_task_id,
        task_state=result.task_state,
        cost_usdc=result.usage_event.cost_usdc,
        settlement_amount_usdc=result.settlement_amount_usdc,
        settlement_tx=result.settlement_tx,
        billing_method=billing.billing_method if billing.verified else "",
        duration_ms=result.duration_ms,
        error=result.output_text if result.task_state != "completed" else "",
    )

    return _build_task_response(
        request_body=body,
        task_id=resolved_task_id,
        task_state=result.response_state,
        output_text="Task failed." if result.task_state == "timeout" else result.output_text,
        rpc_id=rpc_id,
    )
