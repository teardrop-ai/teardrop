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
