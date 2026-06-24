# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""External MCP server registration and tool-discovery routes for an org."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mcp_client import (
    OrgMcpServer,
    create_org_mcp_server,
    delete_org_mcp_server,
    discover_mcp_tools,
    get_org_mcp_server,
    list_org_mcp_servers,
    update_org_mcp_server,
)
from mcp_client.runtime import build_mcp_backed_tool
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.rate_limit import _enforce_rate_limit

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── MCP Server Management Endpoints ─────────────────────────────────────────


class CreateMcpServerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    url: str = Field(..., max_length=2048)
    auth_type: str = Field(default="none", pattern=r"^(none|bearer|header)$")
    auth_token: str | None = Field(default=None, max_length=8192)
    auth_header_name: str | None = Field(default=None, max_length=64)
    timeout_seconds: int = Field(default=15, ge=1, le=60)


class UpdateMcpServerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    url: str | None = Field(default=None, max_length=2048)
    auth_type: str | None = Field(default=None, pattern=r"^(none|bearer|header)$")
    auth_token: str | None = None
    auth_header_name: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    is_active: bool | None = None


def _mcp_server_to_response(srv: OrgMcpServer) -> dict[str, Any]:
    """Convert an OrgMcpServer model to a JSON-serialisable dict."""
    return {
        "id": srv.id,
        "org_id": srv.org_id,
        "name": srv.name,
        "url": srv.url,
        "auth_type": srv.auth_type,
        "has_auth": srv.has_auth,
        "auth_header_name": srv.auth_header_name,
        "is_active": srv.is_active,
        "timeout_seconds": srv.timeout_seconds,
        "created_at": srv.created_at.isoformat(),
        "updated_at": srv.updated_at.isoformat(),
    }


@router.post("/mcp/servers", tags=["MCP"])
async def create_mcp_server(
    body: CreateMcpServerRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Register an external MCP server for the authenticated org."""
    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")

    # Auth consistency
    if body.auth_type == "header" and not body.auth_header_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth_header_name is required when auth_type is 'header'.",
        )
    if body.auth_type != "none" and not body.auth_token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth_token is required when auth_type is not 'none'.",
        )

    try:
        srv = await create_org_mcp_server(
            org_id,
            name=body.name,
            url=body.url,
            auth_type=body.auth_type,
            auth_token=body.auth_token,
            auth_header_name=body.auth_header_name,
            timeout_seconds=body.timeout_seconds,
            actor_id=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_mcp_server_to_response(srv))


@router.get("/mcp/servers", tags=["MCP"])
async def list_mcp_servers(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List MCP servers for the authenticated org."""
    org_id = _require_org_id(payload)
    servers = await list_org_mcp_servers(org_id)
    return JSONResponse(content=[_mcp_server_to_response(s) for s in servers])


@router.get("/mcp/servers/{server_id}", tags=["MCP"])
async def get_mcp_server(
    server_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get a specific MCP server by ID."""
    org_id = _require_org_id(payload)
    srv = await get_org_mcp_server(server_id, org_id)
    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")
    return JSONResponse(content=_mcp_server_to_response(srv))


@router.patch("/mcp/servers/{server_id}", tags=["MCP"])
async def patch_mcp_server(
    server_id: str,
    body: UpdateMcpServerRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Update an MCP server (partial update)."""
    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")

    kwargs: dict[str, Any] = {}
    _mcp_updatable = (
        "name",
        "url",
        "auth_type",
        "auth_token",
        "auth_header_name",
        "timeout_seconds",
        "is_active",
    )
    for field_name in _mcp_updatable:
        val = getattr(body, field_name, None)
        if val is not None:
            kwargs[field_name] = val

    if not kwargs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    try:
        srv = await update_org_mcp_server(server_id, org_id, actor_id=user_id, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")
    return JSONResponse(content=_mcp_server_to_response(srv))


@router.delete("/mcp/servers/{server_id}", tags=["MCP"])
async def remove_mcp_server(
    server_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Soft-delete an MCP server."""
    org_id = _require_org_id(payload)
    user_id: str = payload.get("sub", "")
    deleted = await delete_org_mcp_server(server_id, org_id, actor_id=user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")
    return JSONResponse(content={"status": "deleted"})


@router.post("/mcp/servers/{server_id}/discover", tags=["MCP"])
async def discover_mcp_server_tools(
    server_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Connect to an MCP server and return its available tools."""
    org_id = _require_org_id(payload)
    s = get_settings()
    await _enforce_rate_limit(
        f"mcp:discover:{org_id}",
        s.rate_limit_mcp_discover_rpm,
        detail="Rate limit exceeded for MCP server discovery.",
    )
    srv = await get_org_mcp_server(server_id, org_id)
    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")
    try:
        tools = await discover_mcp_tools(srv)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to MCP server: {type(exc).__name__}",
        )
    return JSONResponse(content={"server_id": server_id, "tools": tools})


# ─── Pre-publish MCP tool diagnostic probe ───────────────────────────────────


class TestMcpToolRequest(BaseModel):
    """Pre-publish MCP tool diagnostic probe.

    Used by the dashboard wizard to verify an MCP tool actually executes
    before the author publishes it as a marketplace listing. Performs a real
    ``call_tool`` against the author's own registered MCP server. Unbilled,
    does not write to the audit trail, and does not interact with the circuit
    breaker — mirroring ``POST /tools/test-webhook`` semantics.
    """

    tool_name: str = Field(..., min_length=1, max_length=128, description="Raw upstream MCP tool name")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments to pass to the tool")


class TestMcpToolResponse(BaseModel):
    """Diagnostic result of a test MCP tool invocation.

    The HTTP status of this endpoint is always 200 on a successful proxy
    attempt; the tool's own success/failure is reported in ``success``.
    ``success=False`` covers upstream errors, bad arguments, and tools that
    return an ``{"error": ...}`` payload.
    """

    success: bool
    latency_ms: int
    result: dict[str, Any] | None
    error: str | None


@router.post("/mcp/servers/{server_id}/test-tool", tags=["MCP"])
async def test_mcp_tool(
    server_id: str,
    body: TestMcpToolRequest,
    payload: dict = Depends(require_auth),
) -> TestMcpToolResponse:
    """Fire a single diagnostic call against an MCP tool on the org's server.

    Resolves the tool's live schema via ``discover_mcp_tools``, wraps it with
    ``build_mcp_backed_tool`` (``tool_id=None`` so the breaker/audit paths are
    bypassed), and invokes it with the supplied ``args``. SSRF pinning and
    response truncation are inherited from the MCP session pool and runtime.
    """
    import time

    org_id = _require_org_id(payload)
    s = get_settings()
    await _enforce_rate_limit(
        f"mcp:test:{org_id}",
        s.rate_limit_mcp_discover_rpm,
        detail="Rate limit exceeded for MCP tool test.",
    )

    srv = await get_org_mcp_server(server_id, org_id)
    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")

    try:
        discovered_tools = await discover_mcp_tools(srv)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to MCP server: {type(exc).__name__}",
        )

    discovered_by_name = {str(t.get("name")): t for t in discovered_tools}
    discovered_tool = discovered_by_name.get(body.tool_name)
    if discovered_tool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tool '{body.tool_name}' not found on MCP server.",
        )

    input_schema = discovered_tool.get("input_schema") or {}
    output_schema = discovered_tool.get("output_schema")
    description = str(discovered_tool.get("description") or "")

    # tool_id=None + on_success/on_failure=None → no breaker, no audit, no billing.
    lc_tool = build_mcp_backed_tool(
        srv,
        body.tool_name,
        body.tool_name,
        description,
        input_schema,
        output_schema=output_schema if isinstance(output_schema, dict) else None,
        tool_id=None,
        on_success=None,
        on_failure=None,
    )

    started = time.monotonic()
    try:
        result = await lc_tool.ainvoke(body.args)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return TestMcpToolResponse(
            success=False,
            latency_ms=latency_ms,
            result=None,
            error=f"{type(exc).__name__}: invocation failed",
        )

    latency_ms = int((time.monotonic() - started) * 1000)

    # build_mcp_backed_tool returns {"error": ...} on upstream failure without
    # raising; treat that as a failed probe so authors see the problem.
    if isinstance(result, dict) and "error" in result and "result" not in result:
        return TestMcpToolResponse(
            success=False,
            latency_ms=latency_ms,
            result=None,
            error=str(result.get("error") or "upstream error"),
        )

    return TestMcpToolResponse(
        success=True,
        latency_ms=latency_ms,
        result=result if isinstance(result, dict) else {"result": result},
        error=None,
    )
