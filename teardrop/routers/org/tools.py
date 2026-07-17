# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Custom webhook-backed tool CRUD and pre-publish webhook diagnostic routes."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from marketplace.models import MarketplaceCategory
from org_tools import (
    OrgTool,
    create_org_tool,
    delete_org_tool,
    get_org_tool,
    invalidate_org_tools_cache,
    list_org_tools,
    normalize_webhook_response,
    update_org_tool,
    validate_safe_schema_subset,
)
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.rate_limit import _enforce_rate_limit
from tools import registry

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL against SSRF rules and HTTPS enforcement.

    Raises HTTP 422 if the URL is unsafe or not HTTPS in production.
    """
    from tools.definitions.http_fetch import validate_url  # noqa: PLC0415

    ssrf_err = validate_url(url)
    if ssrf_err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsafe webhook URL: {ssrf_err}",
        )
    s = get_settings()
    if s.app_env == "production" and not url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Webhook URL must use HTTPS in production.",
        )


# ─── Custom Tool CRUD endpoints ──────────────────────────────────────────────


class CreateOrgToolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(..., min_length=1, max_length=500)
    input_schema: dict = Field(..., description="JSON Schema for tool input parameters")
    output_schema: dict | None = Field(default=None, description="Optional JSON Schema for tool output")
    webhook_url: str = Field(..., max_length=2048)
    webhook_method: Literal["GET"] = "GET"
    auth_header_name: str | None = Field(default=None, max_length=64)
    auth_header_value: str | None = Field(default=None, max_length=4096)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    publish_as_mcp: bool = False
    marketplace_description: str | None = Field(default=None, max_length=1000)
    category: MarketplaceCategory = ""
    base_price_usdc: int = Field(default=0, ge=0, le=100_000_000)


class UpdateOrgToolRequest(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    input_schema: dict | None = None
    output_schema: dict | None = None
    webhook_url: str | None = Field(default=None, max_length=2048)
    auth_header_name: str | None = None
    auth_header_value: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)
    is_active: bool | None = None
    publish_as_mcp: bool | None = None
    marketplace_description: str | None = Field(default=None, max_length=1000)
    category: MarketplaceCategory | None = None
    base_price_usdc: int | None = Field(default=None, ge=0, le=100_000_000)


class OrgToolResponse(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    input_schema: dict
    output_schema: dict | None
    webhook_url: str | None
    webhook_method: str
    mcp_server_id: str | None
    mcp_tool_name: str | None
    has_auth: bool
    timeout_seconds: int
    is_active: bool
    publish_as_mcp: bool
    marketplace_description: str
    category: str
    base_price_usdc: int
    created_at: str
    updated_at: str


class TestWebhookRequest(BaseModel):
    """Pre-publish webhook diagnostic probe.

    Used by the dashboard wizard to validate a webhook URL is reachable and
    returns valid JSON before the tool is created. Plaintext auth header values
    are accepted because the encrypted-at-rest header does not yet exist
    (the tool row has not been created).
    """

    webhook_url: str = Field(..., max_length=2048)
    webhook_method: str = Field(default="POST", pattern=r"^(GET|POST|PUT)$")
    payload: dict = Field(default_factory=dict)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    auth_header_name: str | None = Field(default=None, max_length=64)
    auth_header_value: str | None = Field(default=None, max_length=4096)


class TestWebhookResponse(BaseModel):
    """Diagnostic result of a test webhook invocation.

    The HTTP status of this endpoint is always 200 on a successful proxy
    attempt; the webhook's own status is reported in ``status_code``.
    ``success=True`` requires HTTP 2xx + valid JSON body from the webhook.
    """

    success: bool
    status_code: int | None
    latency_ms: int
    response_body: dict | None
    error: str | None


def _org_tool_to_response(tool: OrgTool) -> dict[str, Any]:
    """Convert an OrgTool model to a JSON-serialisable dict."""
    return {
        "id": tool.id,
        "org_id": tool.org_id,
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "webhook_url": tool.webhook_url,
        "webhook_method": tool.webhook_method,
        "mcp_server_id": tool.mcp_server_id,
        "mcp_tool_name": tool.mcp_tool_name,
        "has_auth": tool.has_auth,
        "timeout_seconds": tool.timeout_seconds,
        "is_active": tool.is_active,
        "publish_as_mcp": tool.publish_as_mcp,
        "marketplace_description": tool.marketplace_description,
        "category": tool.category,
        "base_price_usdc": tool.base_price_usdc,
        "created_at": tool.created_at.isoformat(),
        "updated_at": tool.updated_at.isoformat(),
    }


@router.post("/tools", tags=["Tools"], response_model=OrgToolResponse, status_code=status.HTTP_201_CREATED)
async def create_tool(
    body: CreateOrgToolRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Register a custom webhook-backed tool for the authenticated org."""
    from jsonschema import Draft7Validator, SchemaError  # noqa: PLC0415

    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")

    # Validate JSON Schema
    try:
        Draft7Validator.check_schema(body.input_schema)
    except SchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid input_schema: {exc.message}",
        )

    subset_errors = validate_safe_schema_subset(body.input_schema)
    if subset_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported input_schema features: {'; '.join(subset_errors[:5])}",
        )

    if body.output_schema is not None:
        try:
            Draft7Validator.check_schema(body.output_schema)
        except SchemaError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid output_schema: {exc.message}",
            )
        out_subset_errors = validate_safe_schema_subset(body.output_schema)
        if out_subset_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported output_schema features: {'; '.join(out_subset_errors[:5])}",
            )

    if body.publish_as_mcp and body.output_schema is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="output_schema is required when publish_as_mcp is true.",
        )

    _validate_webhook_url(body.webhook_url)

    # Global name collision check
    if registry.get(body.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tool name '{body.name}' conflicts with a built-in tool.",
        )

    # Auth header consistency
    if body.auth_header_value and not body.auth_header_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth_header_name is required when auth_header_value is provided.",
        )

    try:
        tool = await create_org_tool(
            org_id=org_id,
            name=body.name,
            description=body.description,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            webhook_url=body.webhook_url,
            auth_header_name=body.auth_header_name,
            auth_header_value=body.auth_header_value,
            timeout_seconds=body.timeout_seconds,
            actor_id=user_id,
            publish_as_mcp=body.publish_as_mcp,
            marketplace_description=body.marketplace_description or "",
            category=body.category,
            base_price_usdc=body.base_price_usdc,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tool '{body.name}' already exists for this org.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    await invalidate_org_tools_cache(org_id)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_org_tool_to_response(tool))


@router.get("/tools", tags=["Tools"], response_model=list[OrgToolResponse])
async def list_tools(
    payload: dict = Depends(require_auth),
    active_only: bool = Query(default=True, description="When false, includes inactive (paused) tools in the response."),
) -> JSONResponse:
    """List custom tools for the authenticated org."""
    org_id = _require_org_id(payload)
    tools = await list_org_tools(org_id, active_only=active_only)
    return JSONResponse(content=[_org_tool_to_response(t) for t in tools])


@router.get("/tools/{tool_id}", tags=["Tools"], response_model=OrgToolResponse)
async def get_tool(
    tool_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get a specific custom tool by ID."""
    org_id = _require_org_id(payload)
    tool = await get_org_tool(tool_id, org_id)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found.")
    return JSONResponse(content=_org_tool_to_response(tool))


@router.patch("/tools/{tool_id}", tags=["Tools"], response_model=OrgToolResponse)
async def patch_tool(
    tool_id: str,
    body: UpdateOrgToolRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Update a custom tool (partial update)."""
    from jsonschema import Draft7Validator, SchemaError  # noqa: PLC0415

    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")

    # SSRF check if webhook_url is being changed
    if body.webhook_url is not None:
        _validate_webhook_url(body.webhook_url)

    if body.input_schema is not None:
        try:
            Draft7Validator.check_schema(body.input_schema)
        except SchemaError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid input_schema: {exc.message}",
            )
        subset_errors = validate_safe_schema_subset(body.input_schema)
        if subset_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported input_schema features: {'; '.join(subset_errors[:5])}",
            )

    if body.output_schema is not None:
        try:
            Draft7Validator.check_schema(body.output_schema)
        except SchemaError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid output_schema: {exc.message}",
            )
        out_subset_errors = validate_safe_schema_subset(body.output_schema)
        if out_subset_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported output_schema features: {'; '.join(out_subset_errors[:5])}",
            )

    if body.publish_as_mcp is True and body.output_schema is None:
        current_tool = await get_org_tool(tool_id, org_id)
        if current_tool is not None and current_tool.output_schema is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="output_schema is required when publish_as_mcp is true.",
            )

    kwargs: dict[str, Any] = {}
    _updatable = (
        "description",
        "input_schema",
        "output_schema",
        "webhook_url",
        "auth_header_name",
        "auth_header_value",
        "timeout_seconds",
        "is_active",
        "publish_as_mcp",
        "marketplace_description",
        "category",
        "base_price_usdc",
    )
    for field_name in _updatable:
        val = getattr(body, field_name, None)
        if val is not None:
            kwargs[field_name] = val

    if not kwargs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    tool = await update_org_tool(tool_id, org_id, actor_id=user_id, **kwargs)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found.")

    await invalidate_org_tools_cache(org_id)
    return JSONResponse(content=_org_tool_to_response(tool))


class ToolDeletedResponse(BaseModel):
    status: Literal["deleted"]


@router.delete("/tools/{tool_id}", tags=["Tools"], response_model=ToolDeletedResponse)
async def remove_tool(
    tool_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Soft-delete a custom tool."""
    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")
    deleted = await delete_org_tool(tool_id, org_id, actor_id=user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found.")
    await invalidate_org_tools_cache(org_id)
    return JSONResponse(content={"status": "deleted"})


@router.post("/tools/test-webhook", tags=["Tools"])
async def test_webhook(
    body: TestWebhookRequest,
    payload: dict = Depends(require_auth),
) -> TestWebhookResponse:
    """Fire a single diagnostic call against an author's webhook URL.

    Used by the dashboard publishing wizard to verify a webhook is reachable
    and returns valid JSON before the tool is created. Does not write to the
    audit trail and does not interact with the circuit breaker — this is a
    developer probe, not a financial event.

    Always returns HTTP 200 on a successful proxy attempt; the webhook's own
    HTTP status is reported in the response body's ``status_code`` field.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    import aiohttp  # noqa: PLC0415

    from tools.definitions.http_fetch import (  # noqa: PLC0415
        async_validate_url_with_ips,
        make_ssrf_safe_connector,
    )

    org_id = _require_org_id(payload)

    # Per-org rate limit — defends against proxy abuse via a stolen JWT.
    s = get_settings()
    await _enforce_rate_limit(
        f"test_webhook:{org_id}",
        s.rate_limit_test_webhook_rpm,
        detail="Rate limit exceeded for /tools/test-webhook.",
    )

    # Sync SSRF + HTTPS-in-prod validation (raises 422 on fail).
    _validate_webhook_url(body.webhook_url)

    # Async DNS re-check (anti-rebinding) — returns the exact validated IPs so the
    # connection below can be pinned, closing the DNS-rebinding TOCTOU window.
    url_err, validated_ips = await async_validate_url_with_ips(body.webhook_url)
    if url_err is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsafe webhook URL: {url_err}",
        )

    # Auth header consistency check (mirrors create_tool).
    if body.auth_header_value and not body.auth_header_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth_header_name is required when auth_header_value is provided.",
        )

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if body.auth_header_name and body.auth_header_value:
        headers[body.auth_header_name] = body.auth_header_value

    timeout = aiohttp.ClientTimeout(total=body.timeout_seconds)
    started = time.monotonic()

    hostname = urlparse(body.webhook_url).hostname or ""
    connector = make_ssrf_safe_connector(hostname, validated_ips)

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            if body.webhook_method == "GET":
                resp = await session.get(body.webhook_url, headers=headers, params=body.payload)
            elif body.webhook_method == "PUT":
                resp = await session.put(body.webhook_url, headers=headers, json=body.payload)
            else:
                resp = await session.post(body.webhook_url, headers=headers, json=body.payload)

            raw = await resp.read()
            latency_ms = int((time.monotonic() - started) * 1000)
            content_type = resp.headers.get("Content-Type", "")
            normalized = normalize_webhook_response(
                raw,
                content_type=content_type,
                status_code=resp.status,
                max_bytes=4096,
            )

            if not normalized["success"]:
                return TestWebhookResponse(
                    success=False,
                    status_code=normalized["status_code"],
                    latency_ms=latency_ms,
                    response_body=normalized["response_body"],
                    error=normalized["error"],
                )

            return TestWebhookResponse(
                success=True,
                status_code=normalized["status_code"],
                latency_ms=latency_ms,
                response_body=normalized["response_body"],
                error=None,
            )

    except asyncio.TimeoutError:
        latency_ms = int((time.monotonic() - started) * 1000)
        return TestWebhookResponse(
            success=False,
            status_code=None,
            latency_ms=latency_ms,
            response_body=None,
            error=f"Webhook timed out after {body.timeout_seconds}s",
        )
    except aiohttp.ClientConnectorError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return TestWebhookResponse(
            success=False,
            status_code=None,
            latency_ms=latency_ms,
            response_body=None,
            error=f"Connection failed: {exc.__class__.__name__}",
        )
    except aiohttp.ClientError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return TestWebhookResponse(
            success=False,
            status_code=None,
            latency_ms=latency_ms,
            response_body=None,
            error=f"HTTP client error: {exc.__class__.__name__}",
        )
