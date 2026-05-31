# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Admin tool & MCP server inspection for a given org.

All routes require the ``require_admin`` dependency. Extracted verbatim from
``teardrop.routers.admin`` with no logic changes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from mcp_client import list_org_mcp_servers
from org_tools import list_org_tools
from teardrop.dependencies import require_admin
from teardrop.routers.org.mcp import _mcp_server_to_response
from teardrop.routers.org.tools import _org_tool_to_response

router = APIRouter()


@router.get("/admin/tools/{org_id}", tags=["Admin", "Admin / Tools"])
async def admin_list_tools(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list all custom tools for a given org (including inactive)."""
    tools = await list_org_tools(org_id, active_only=False)
    return JSONResponse(content=[_org_tool_to_response(t) for t in tools])


@router.get("/admin/mcp/servers/{org_id}", tags=["Admin", "Admin / Tools"])
async def admin_list_mcp_servers(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list all MCP servers for an org."""
    servers = await list_org_mcp_servers(org_id, active_only=False)
    return JSONResponse(content=[_mcp_server_to_response(s) for s in servers])
