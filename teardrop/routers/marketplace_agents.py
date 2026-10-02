# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace A2A agent routes: org agent-registration CRUD and the public agent directory.

Extracted verbatim from ``teardrop.routers.marketplace`` with no logic changes.
URL validation (SSRF) for registered agent URLs lives in ``marketplace.agents``.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from marketplace import (
    _build_agent_cursor,
    _decode_agent_cursor,
    delete_agent_registration,
    get_agent_directory,
    get_agent_registration,
    preview_agent_registration,
    probe_agent_registration,
    set_agent_registration,
)
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth, require_scope
from teardrop.rate_limit import _enforce_rate_limit

router = APIRouter()

_AGENT_DIRECTORY_VALID_SORTS = frozenset({"name", "reputation"})
_AGENT_DIRECTORY_VALID_STALE_FILTERS = frozenset({"all", "active", "stale"})


class MarketplaceAgentRegistrationRequest(BaseModel):
    agent_url: str = Field(..., min_length=1, max_length=2048)


class MarketplaceAgentRegistrationResponse(BaseModel):
    org_id: str
    agent_url: str
    created_at: str
    updated_at: str


class MarketplaceAgentRegistrationPreviewResponse(BaseModel):
    registrable: bool
    detail: str | None = Field(default=None, description="The error PUT would return; null when registrable.")
    agent_url: str | None = None
    price_per_task_usdc: int | None = None


class MarketplaceAgentRegistrationCheck(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class MarketplaceAgentRegistrationTestResponse(BaseModel):
    passed: bool
    agent_url: str | None = None
    checks: list[MarketplaceAgentRegistrationCheck]


class MarketplaceAgentSummary(BaseModel):
    org_slug: str
    org_name: str
    agent_url: str
    agent_card_url: str
    message_endpoint: str
    catalog_endpoint: str
    tool_count: int
    registered_at: str | None = Field(
        default=None,
        description="UTC timestamp when the organization first registered its A2A endpoint",
    )
    reputation_score: float | None = None
    success_rate: float | None = None
    sample_size: float | None = None
    confidence: float | None = None
    unique_caller_count: int | None = None
    last_event_at: str | None = None
    is_stale: bool | None = None


class MarketplaceAgentDirectoryResponse(BaseModel):
    agents: list[MarketplaceAgentSummary]
    next_cursor: str | None = None


@router.put(
    "/marketplace/agent-registration",
    tags=["Marketplace"],
    response_model=MarketplaceAgentRegistrationResponse,
)
async def set_marketplace_agent_registration(
    body: MarketplaceAgentRegistrationRequest,
    payload: dict = Depends(require_scope("publish")),
) -> JSONResponse:
    """Publish the authenticated organization's A2A endpoint."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"a2a_registration:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )
    try:
        registration = await set_agent_registration(org_id, body.agent_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None

    return JSONResponse(
        content={
            "org_id": registration["org_id"],
            "agent_url": registration["agent_url"],
            "created_at": registration["created_at"].isoformat(),
            "updated_at": registration["updated_at"].isoformat(),
        }
    )


@router.post(
    "/marketplace/agent-registration/preview",
    tags=["Marketplace"],
    response_model=MarketplaceAgentRegistrationPreviewResponse,
)
async def preview_marketplace_agent_registration(
    body: MarketplaceAgentRegistrationRequest,
    payload: dict = Depends(require_scope("publish")),
) -> JSONResponse:
    """Dry-run agent registration: same validation verdict as PUT, with a fresh card fetch and no writes."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"a2a_registration:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )
    return JSONResponse(content=await preview_agent_registration(org_id, body.agent_url))


@router.post(
    "/marketplace/agent-registration/test",
    tags=["Marketplace"],
    response_model=MarketplaceAgentRegistrationTestResponse,
)
async def check_marketplace_agent_registration(
    body: MarketplaceAgentRegistrationRequest,
    payload: dict = Depends(require_scope("publish")),
) -> JSONResponse:
    """Check an agent endpoint with one unpaid, unbilled message. Priced agents must answer 402; nothing is signed."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"a2a_registration_test:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration tests.",
    )
    return JSONResponse(content=await probe_agent_registration(org_id, body.agent_url))


@router.get(
    "/marketplace/agent-registration",
    tags=["Marketplace"],
    response_model=MarketplaceAgentRegistrationResponse,
)
async def get_marketplace_agent_registration(payload: dict = Depends(require_auth)) -> JSONResponse:
    """Return the authenticated organization's A2A endpoint registration."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    registration = await get_agent_registration(_require_org_id(payload))
    if registration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent registration not found.")
    return JSONResponse(
        content={
            "org_id": registration["org_id"],
            "agent_url": registration["agent_url"],
            "created_at": registration["created_at"].isoformat(),
            "updated_at": registration["updated_at"].isoformat(),
        }
    )


@router.delete("/marketplace/agent-registration", tags=["Marketplace"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_marketplace_agent_registration(payload: dict = Depends(require_scope("publish"))) -> Response:
    """Unpublish the authenticated organization's A2A endpoint."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id = _require_org_id(payload)
    await _enforce_rate_limit(
        f"a2a_registration:{org_id}",
        s.rate_limit_auth_rpm,
        detail="Rate limit exceeded for A2A agent registration.",
    )
    await delete_agent_registration(org_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/marketplace/agents",
    tags=["Marketplace"],
    response_model=MarketplaceAgentDirectoryResponse,
)
async def list_marketplace_agents_endpoint(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    sort: str = "name",
    stale: str = "all",
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
) -> JSONResponse:
    """Publicly list opt-in A2A endpoints and derived trust metrics.

    ``sort`` accepts ``name`` or ``reputation``; ``stale`` accepts ``all``,
    ``active``, or ``stale``. Privacy-suppressed or untested agents have
    ``is_stale=None`` and are returned only by ``stale=all``. Cursors are
    scoped to both query modes.
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    if sort not in _AGENT_DIRECTORY_VALID_SORTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid agent directory sort. Allowed: name, reputation.",
        )
    if stale not in _AGENT_DIRECTORY_VALID_STALE_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid agent directory stale filter. Allowed: active, all, stale.",
        )

    client_ip = request.client.host if request.client else "unknown"
    await _enforce_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)

    cursor_data = _decode_agent_cursor(cursor)
    if cursor and cursor_data is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid agent directory cursor.")
    if cursor_data is not None and cursor_data[0] != sort:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent directory cursor does not match the requested sort.",
        )
    if cursor_data is not None and cursor_data[3] != stale:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Agent directory cursor does not match the requested stale filter.",
        )
    cursor_key: Any = None
    cursor_slug: str | None = None
    if cursor_data is not None:
        _, cursor_key, cursor_slug, _ = cursor_data

    search = q.strip().casefold() if q else ""
    snapshot = await get_agent_directory()
    candidates: list[dict[str, Any]] = []
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict):
            continue
        org_slug = str(agent.get("org_slug", ""))
        org_name = str(agent.get("org_name", ""))
        agent_is_stale = agent.get("is_stale")
        if stale != "all" and agent_is_stale != (stale == "stale"):
            continue
        if search and search not in org_slug.casefold() and search not in org_name.casefold():
            continue
        agent_url = str(agent["agent_url"])
        candidates.append(
            {
                **agent,
                "agent_card_url": f"{agent_url}/.well-known/agent-card.json",
                "message_endpoint": f"{agent_url}/message:send",
                "catalog_endpoint": f"/marketplace/catalog?org_slug={org_slug}",
                "last_event_at": agent.get("last_event_at"),
                "is_stale": agent_is_stale,
            }
        )

    def reputation_score(agent: dict[str, Any]) -> float | None:
        value = agent.get("reputation_score")
        if value is None or isinstance(value, bool):
            return None
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return score if math.isfinite(score) else None

    if sort == "reputation":

        def reputation_sort_key(agent: dict[str, Any]) -> tuple[bool, float, str]:
            score = reputation_score(agent)
            return (score is None, -(score or 0.0), str(agent["org_slug"]))

        candidates.sort(key=reputation_sort_key)
    else:
        candidates.sort(key=lambda agent: str(agent["org_slug"]))

    if cursor_data is not None and cursor_slug is not None:
        if sort == "name":
            candidates = [agent for agent in candidates if str(agent["org_slug"]) > cursor_slug]
        else:
            cursor_score = float(cursor_key) if cursor_key is not None else None
            filtered_candidates: list[dict[str, Any]] = []
            for agent in candidates:
                agent_slug = str(agent["org_slug"])
                agent_score = reputation_score(agent)
                if cursor_score is None:
                    is_after = agent_score is None and agent_slug > cursor_slug
                else:
                    is_after = (
                        agent_score is None
                        or agent_score < cursor_score
                        or (agent_score == cursor_score and agent_slug > cursor_slug)
                    )
                if is_after:
                    filtered_candidates.append(agent)
            candidates = filtered_candidates

    page = candidates[:limit]
    next_cursor = _build_agent_cursor(page[-1], sort, stale) if len(page) == limit else None
    return JSONResponse(
        content={"agents": page, "next_cursor": next_cursor},
        headers={"Cache-Control": "public, max-age=60"},
    )
