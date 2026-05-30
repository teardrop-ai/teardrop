# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""System & discovery routes: root redirect, health, JWKS, A2A/MCP cards."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from org_tools import list_marketplace_tools
from teardrop._meta import APP_VERSION
from teardrop.cache import get_redis
from teardrop.config import get_settings
from tools import registry

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@router.get("/health", tags=["System"])
async def health_check(request: Request) -> JSONResponse:
    """Liveness probe – returns service status, version, and DB connectivity."""
    pool: asyncpg.Pool | None = getattr(request.app.state, "pool", None)
    if pool is not None:
        try:
            await pool.execute("SELECT 1")
            postgres = "ok"
        except Exception:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "degraded",
                    "service": "teardrop",
                    "version": APP_VERSION,
                    "environment": settings.app_env,
                    "postgres": "error",
                },
            )
    else:
        postgres = "starting"

    # Redis status.
    redis = get_redis()
    if redis is not None:
        try:
            await redis.ping()
            redis_status = "ok"
        except Exception:
            redis_status = "error"
    else:
        redis_status = "disabled"

    overall = "ok" if postgres == "ok" and redis_status != "error" else "degraded"
    return JSONResponse(
        content={
            "status": overall,
            "service": "teardrop",
            "version": APP_VERSION,
            "environment": settings.app_env,
            "postgres": postgres,
            "redis": redis_status,
        }
    )


@router.get("/.well-known/jwks.json", tags=["System"])
async def jwks() -> JSONResponse:
    """Expose the RS256 public key in JWKS format for external JWT verification."""
    import base64

    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(settings.jwt_public_key.encode())
    nums = pub.public_numbers()  # type: ignore[union-attr]

    def _b64url(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    return JSONResponse(
        content={
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": "teardrop-rs256",
                    "n": _b64url(nums.n),
                    "e": _b64url(nums.e),
                }
            ],
        }
    )


@router.get("/.well-known/agent-card.json", tags=["A2A"])
async def agent_card() -> JSONResponse:
    """A2A agent card for discoverability and inter-agent communication."""
    card_settings = get_settings()
    capabilities: dict[str, Any] = {
        "streaming": True,
        "a2ui": True,
        "mcp_tools": True,
        "multi_turn": True,
        "human_in_the_loop": True,
        "billing": {
            "enabled": card_settings.billing_enabled,
            "scheme": card_settings.x402_scheme,
            "network": card_settings.x402_network,
            "payment_endpoint": "/agent/run",
            "pricing_endpoint": "/billing/pricing",
            **(
                {
                    "max_amount": card_settings.x402_upto_max_amount,
                }
                if card_settings.x402_scheme == "upto"
                else {}
            ),
        },
    }
    endpoints = {
        "agent_run": "/agent/run",
        "health": "/health",
        "docs": "/docs",
        "mcp_tools": "/tools/mcp",
    }
    if card_settings.marketplace_enabled:
        capabilities["marketplace"] = {
            "enabled": True,
            "catalog_endpoint": "/marketplace/catalog",
            "mcp_gateway_endpoint": endpoints["mcp_tools"],
        }
        endpoints["marketplace_catalog"] = "/marketplace/catalog"
    return JSONResponse(
        content={
            "schema_version": "1.0",
            "name": "Teardrop",
            "description": (
                "Intelligence beyond the browser. A task-manager agent with LangGraph, AG-UI streaming, and A2UI rendering."
            ),
            "version": APP_VERSION,
            "url": f"http://{card_settings.app_host}:{card_settings.app_port}",
            "capabilities": capabilities,
            "protocols": ["ag-ui", "a2a", "mcp"],
            "endpoints": endpoints,
            "skills": [
                {
                    "name": "task_planning",
                    "description": "Break complex tasks into actionable steps.",
                },
                *registry.to_a2a_skills(),
                {
                    "name": "a2ui_rendering",
                    "description": "Declarative UI component generation (table, form, text, button, etc.).",  # noqa: E501
                },
            ],
            "tools": registry.to_a2a_tool_list(),
            "authentication": {
                "required": True,
                "scheme": "bearer",
                "type": "jwt",
                "token_endpoint": "/token",
            },
        }
    )


@router.get("/.well-known/mcp/server-card.json", tags=["MCP"])
async def mcp_server_card() -> JSONResponse:
    """Static MCP server card for Smithery and other MCP registries."""
    tools = [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema.model_json_schema(),
        }
        for t in registry.list_latest()
    ]
    # Include published marketplace tools
    s = get_settings()
    if s.marketplace_enabled:
        try:
            mp_tools = await list_marketplace_tools()
            for mt in mp_tools:
                tools.append(
                    {
                        "name": mt.name,
                        "description": mt.marketplace_description or mt.description,
                        "inputSchema": mt.input_schema,
                    }
                )
        except Exception:
            logger.debug("Failed to load marketplace tools for server card", exc_info=True)
    return JSONResponse(
        content={
            "serverInfo": {"name": "teardrop-tools", "version": APP_VERSION},
            "authentication": {"required": True, "schemes": ["bearer"]},
            "tools": tools,
            "resources": [],
            "prompts": [],
        }
    )
