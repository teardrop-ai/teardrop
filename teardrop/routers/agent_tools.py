# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Agent tool inventory and exclusion routes."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import get_current_pricing, get_tool_pricing_overrides
from marketplace import get_marketplace_catalog, get_subscribed_tools_catalog
from org_tools import list_org_tools
from teardrop.agent_schemas import _normalize_exclusion_name
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.tool_exclusions import add_org_tool_exclusion, list_org_tool_exclusions, remove_org_tool_exclusion

router = APIRouter()


class AgentToolItem(BaseModel):
    name: str
    qualified_name: str
    source: Literal["platform", "org", "marketplace"]
    access_mode: Literal["included", "subscribed"]
    display_name: str
    description: str
    cost_usdc: int
    input_schema: dict[str, Any]


@router.get("/agent/tools", tags=["Agent"], response_model=list[AgentToolItem])
async def list_agent_tools(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return all tools available to the authenticated org's agent runs."""
    org_id = _require_org_id(payload)
    settings = get_settings()

    tool_overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0

    if settings.marketplace_enabled:
        platform_tools, org_tools, subscribed_tools = await asyncio.gather(
            get_marketplace_catalog(tool_overrides, default_cost, org_slug="platform"),
            list_org_tools(org_id),
            get_subscribed_tools_catalog(org_id, tool_overrides, default_cost),
        )
    else:
        org_tools = await list_org_tools(org_id)
        platform_tools = []
        subscribed_tools = []

    tools: list[AgentToolItem] = []

    for tool in platform_tools:
        tools.append(
            AgentToolItem(
                name=tool.name,
                qualified_name=tool.qualified_name,
                source="platform",
                access_mode="included",
                display_name=tool.display_name or tool.name,
                description=tool.marketplace_description or tool.description,
                cost_usdc=tool.cost_usdc,
                input_schema=tool.input_schema,
            )
        )

    for tool in org_tools:
        if not tool.is_active:
            continue
        qualified_name = f"org/{tool.name}"
        cost_usdc = tool_overrides.get(qualified_name, tool_overrides.get(tool.name, 0))
        tools.append(
            AgentToolItem(
                name=tool.name,
                qualified_name=qualified_name,
                source="org",
                access_mode="included",
                display_name=tool.name,
                description=tool.description,
                cost_usdc=cost_usdc,
                input_schema=tool.input_schema,
            )
        )

    for tool in subscribed_tools:
        tools.append(
            AgentToolItem(
                name=tool.name,
                qualified_name=tool.qualified_name,
                source="marketplace",
                access_mode="subscribed",
                display_name=tool.display_name or tool.name,
                description=tool.marketplace_description or tool.description,
                cost_usdc=tool.cost_usdc,
                input_schema=tool.input_schema,
            )
        )

    return JSONResponse(content={"tools": [t.model_dump() for t in tools]})


class ToolExclusionRequest(BaseModel):
    tool_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Internal tool name to exclude (unprefixed, e.g. 'web_search', not 'platform/web_search').",
    )


class ToolExclusionListResponse(BaseModel):
    tool_names: list[str] = Field(..., description="Persisted tool exclusions for the authenticated org.")


class ToolExclusionActionResponse(BaseModel):
    status: Literal["added"] = Field(..., description="Outcome of the exclusion write.")
    tool_name: str = Field(..., description="Normalized (unprefixed) tool name that was excluded.")


class ToolExclusionRemovedResponse(BaseModel):
    status: Literal["removed"]
    tool_name: str = Field(..., description="Normalized (unprefixed) tool name that was removed.")


@router.get("/agent/tool-exclusions", tags=["Agent"], response_model=ToolExclusionListResponse)
async def get_agent_tool_exclusions(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List the authenticated org's persisted tool exclusions."""
    org_id = _require_org_id(payload, "No org_id in token — tool exclusions require an org-scoped credential.")
    tool_names = await list_org_tool_exclusions(org_id)
    return JSONResponse(content={"tool_names": tool_names})


@router.post("/agent/tool-exclusions", tags=["Agent"], response_model=ToolExclusionActionResponse)
async def create_agent_tool_exclusion(
    body: ToolExclusionRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Persist a tool exclusion for the authenticated org."""
    org_id = _require_org_id(payload, "No org_id in token — tool exclusions require an org-scoped credential.")
    normalized = _normalize_exclusion_name(body.tool_name.strip())
    try:
        await add_org_tool_exclusion(org_id, normalized)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return JSONResponse(content={"status": "added", "tool_name": normalized})


@router.delete("/agent/tool-exclusions/{tool_name}", tags=["Agent"], response_model=ToolExclusionRemovedResponse)
async def delete_agent_tool_exclusion(
    tool_name: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Remove a persisted tool exclusion for the authenticated org."""
    org_id = _require_org_id(payload, "No org_id in token — tool exclusions require an org-scoped credential.")
    normalized = _normalize_exclusion_name(tool_name.strip())
    removed = await remove_org_tool_exclusion(org_id, normalized)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool exclusion not found.")
    return JSONResponse(content={"status": "removed", "tool_name": normalized})
