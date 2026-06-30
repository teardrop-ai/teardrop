# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""System & discovery routes: root redirect, health, JWKS, A2A/MCP cards."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from billing import build_402_response_body
from org_tools import list_marketplace_tools
from teardrop._meta import APP_VERSION
from teardrop.cache import get_redis
from teardrop.config import get_settings
from teardrop.public_url import public_base_url
from tools import registry

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


def _public_base_url(request: Request, current_settings) -> str:
    return public_base_url(request, current_settings)


def _discovery_headers(*, cache_seconds: int, etag: str | None = None) -> dict[str, str]:
    headers = {
        "Cache-Control": f"public, max-age={cache_seconds}",
        "Vary": "Host, X-Forwarded-Host, X-Forwarded-Proto",
    }
    if etag:
        headers["ETag"] = etag
    return headers


def _build_weak_etag(content: Any) -> str:
    serialized = json.dumps(content, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f'W/"{APP_VERSION}-{digest}"'


def _etag_matches(request: Request, etag: str) -> bool:
    if_none_match = request.headers.get("if-none-match", "")
    if not if_none_match:
        return False
    candidates = {item.strip() for item in if_none_match.split(",")}
    return "*" in candidates or etag in candidates


def _json_discovery_response(request: Request, content: dict[str, Any], *, cache_seconds: int = 300) -> Response:
    etag = _build_weak_etag(content)
    headers = _discovery_headers(cache_seconds=cache_seconds, etag=etag)
    if _etag_matches(request, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return JSONResponse(content=content, headers=headers)


def _build_llms_txt(base_url: str, *, marketplace_enabled: bool) -> str:
    lines = [
        "# Teardrop",
        "",
        (
            "> Intelligence beyond the browser. Teardrop is a task-manager agent API with "
            "AG-UI streaming, MCP tool discovery, and optional paid marketplace access."
        ),
        "",
        "Use these public discovery surfaces before authenticating or invoking paid workflows.",
        "",
        "## Discovery",
        f"- [Agent Card]({base_url}/.well-known/agent-card.json): Public Teardrop agent discovery manifest.",
        f"- [Legacy Agent Card]({base_url}/.well-known/agent.json): Legacy alias for older crawlers.",
        f"- [MCP Server Card]({base_url}/.well-known/mcp/server-card.json): Public MCP tool catalogue.",
        f"- [Docs]({base_url}/docs): Interactive API documentation.",
        "",
        "## Pricing",
        f"- [Billing Pricing]({base_url}/billing/pricing): Public pricing and payment metadata.",
    ]
    if marketplace_enabled:
        lines.extend(
            [
                "",
                "## Marketplace",
                f"- [Marketplace Catalog]({base_url}/marketplace/catalog): Public paid MCP tool catalogue.",
                f"- [Marketplace llms.txt]({base_url}/marketplace/llms.txt): LLM-friendly marketplace index.",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _build_agent_card_content(request: Request) -> dict[str, Any]:
    card_settings = get_settings()
    base_url = _public_base_url(request, card_settings)
    security_requirements = [{"bearer_jwt": []}]
    capabilities: dict[str, Any] = {
        "streaming": True,
        "pushNotifications": False,
        "extendedAgentCard": False,
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
    supported_interfaces = [
        {
            "url": f"{base_url}/agent/run",
            "protocolBinding": "https://teardrop.ai/bindings/ag-ui-sse/v1",
            "protocolVersion": "1.0",
        }
    ]
    protocols = ["ag-ui", "mcp"]
    if card_settings.a2a_inbound_enabled:
        endpoints["a2a_message"] = "/message:send"
        supported_interfaces.append(
            {
                "url": f"{base_url}/message:send",
                "protocolBinding": "https://teardrop.ai/bindings/a2a-jsonrpc/v1",
                "protocolVersion": "1.0",
            }
        )
        protocols.insert(1, "a2a")
    if card_settings.marketplace_enabled:
        capabilities["marketplace"] = {
            "enabled": True,
            "catalog_endpoint": "/marketplace/catalog",
            "mcp_gateway_endpoint": endpoints["mcp_tools"],
        }
        endpoints["marketplace_catalog"] = "/marketplace/catalog"

    card: dict[str, Any] = {
        "schema_version": "1.0",
        "protocolVersion": "1.0",
        "name": "Teardrop",
        "description": (
            "Intelligence beyond the browser. A task-manager agent with LangGraph, AG-UI streaming, and A2UI rendering."
        ),
        "version": APP_VERSION,
        "url": base_url,
        "provider": {
            "organization": "Teardrop AI",
            "url": base_url,
        },
        "documentationUrl": f"{base_url}/docs",
        "supportedInterfaces": supported_interfaces,
        "capabilities": capabilities,
        "protocols": protocols,
        "endpoints": endpoints,
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "task_planning",
                "name": "task_planning",
                "description": "Break complex tasks into actionable steps.",
            },
            *registry.to_a2a_skills(),
            {
                "id": "a2ui_rendering",
                "name": "a2ui_rendering",
                "description": "Declarative UI component generation (table, form, text, button, etc.).",
            },
        ],
        "tools": registry.to_a2a_tool_list(),
        "authentication": {
            "required": True,
            "scheme": "bearer",
            "type": "jwt",
            "token_endpoint": "/token",
        },
        "securitySchemes": {
            "bearer_jwt": {
                "httpAuthSecurityScheme": {
                    "description": "JWT bearer token issued by Teardrop.",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            }
        },
        "security": security_requirements,
        "securityRequirements": security_requirements,
    }

    icon_url = getattr(card_settings, "agent_card_icon_url", "").strip()
    if icon_url:
        card["iconUrl"] = icon_url
    return card


def _mcp_server_description() -> str:
    return (
        "The native infrastructure layer for autonomous economic agents. "
        "Teardrop exposes its curated Web3, data, and utility tools "
        "over MCP with public discovery and authenticated execution."
    )


def _build_oauth_protected_resource_content(request: Request, resource_path: str = "") -> dict[str, Any]:
    current_settings = get_settings()
    base_url = _public_base_url(request, current_settings)
    normalized_path = f"/{resource_path.lstrip('/')}" if resource_path else ""
    resource_url = f"{base_url}{normalized_path}"
    resource_name = "Teardrop" if not normalized_path else "Teardrop MCP"

    return {
        "resource": resource_url,
        "resource_name": resource_name,
        "resource_documentation": f"{base_url}/docs",
        "bearer_methods_supported": ["header"],
        "description": _mcp_server_description(),
        "homepage": base_url,
    }


def _build_x402_discovery_content(request: Request) -> dict[str, Any]:
    current_settings = get_settings()
    base_url = _public_base_url(request, current_settings)
    endpoints: dict[str, str] = {
        "pricing": "/billing/pricing",
        "docs": "/docs",
        "agent_run": "/agent/run",
        "mcp_tools": "/tools/mcp",
    }
    resources: list[dict[str, Any]] = [
        {
            "path": "/agent/run",
            "url": f"{base_url}/agent/run",
            "method": "POST",
            "protocol": "ag-ui",
            "auth_modes": ["bearer"],
            "description": "Streaming Teardrop agent execution endpoint.",
        },
        {
            "path": "/tools/mcp",
            "url": f"{base_url}/tools/mcp",
            "method": "POST",
            "protocol": "mcp",
            "auth_modes": ["bearer", *(["x402"] if current_settings.mcp_x402_enabled else [])],
            "description": "MCP discovery and optional paid tool execution gateway.",
        },
    ]
    if current_settings.a2a_inbound_enabled:
        endpoints["a2a_message"] = "/message:send"
        resources.insert(
            1,
            {
                "path": "/message:send",
                "url": f"{base_url}/message:send",
                "method": "POST",
                "protocol": "a2a",
                "auth_modes": ["bearer", *(["x402"] if current_settings.billing_enabled else ["anonymous"])],
                "description": "Blocking public A2A endpoint for external agent callers.",
            },
        )

    accepts: list[dict[str, Any]] = []
    x402_version = 2
    if current_settings.billing_enabled:
        try:
            payment_body = build_402_response_body()
        except RuntimeError:
            logger.debug("x402 discovery requested before billing requirements were initialized", exc_info=True)
        else:
            accepts = payment_body.get("accepts", [])
            x402_version = int(payment_body.get("x402Version", 2))

    return {
        "x402Version": x402_version,
        "accepts": accepts,
        "billing": {
            "enabled": current_settings.billing_enabled,
            "scheme": current_settings.x402_scheme,
            "network": current_settings.x402_network,
            "pricing_endpoint": f"{base_url}/billing/pricing",
        },
        "endpoints": endpoints,
        "resources": resources,
        "homepage": base_url,
        "documentationUrl": f"{base_url}/docs",
    }


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
async def agent_card(request: Request) -> Response:
    """A2A agent card for discoverability and inter-agent communication."""
    return _json_discovery_response(request, _build_agent_card_content(request))


@router.get("/.well-known/x402", tags=["System"])
async def x402_discovery(request: Request) -> Response:
    """Public x402 metadata for registries and validators."""
    return _json_discovery_response(request, _build_x402_discovery_content(request))


@router.get("/.well-known/x402.json", include_in_schema=False, tags=["System"])
async def x402_discovery_json(request: Request) -> Response:
    """Legacy JSON alias for x402 discovery metadata."""
    return _json_discovery_response(request, _build_x402_discovery_content(request))


@router.get("/.well-known/agent.json", include_in_schema=False, tags=["A2A"])
async def legacy_agent_card(request: Request) -> Response:
    """Legacy alias for older discovery clients that still probe agent.json."""
    return _json_discovery_response(request, _build_agent_card_content(request))


@router.get("/.well-known/oauth-protected-resource", tags=["MCP"])
async def oauth_protected_resource_root(request: Request) -> Response:
    """OAuth protected-resource metadata for the Teardrop host resource."""
    return _json_discovery_response(request, _build_oauth_protected_resource_content(request))


@router.get("/.well-known/oauth-protected-resource/{resource_path:path}", tags=["MCP"])
async def oauth_protected_resource_path(request: Request, resource_path: str) -> Response:
    """OAuth protected-resource metadata for path-scoped resources such as /tools/mcp."""
    return _json_discovery_response(request, _build_oauth_protected_resource_content(request, resource_path))


@router.get("/llms.txt", include_in_schema=False, tags=["System"])
async def llms_txt(request: Request) -> Response:
    """Root llms.txt manifest for LLM-friendly Teardrop discovery."""
    current_settings = get_settings()
    base_url = _public_base_url(request, current_settings)
    return Response(
        content=_build_llms_txt(base_url, marketplace_enabled=current_settings.marketplace_enabled),
        media_type="text/plain; charset=utf-8",
        headers=_discovery_headers(cache_seconds=3600),
    )


@router.get("/robots.txt", include_in_schema=False, tags=["System"])
async def robots_txt(request: Request) -> Response:
    """Crawler directives plus a pointer to the llms.txt manifest."""
    base_url = _public_base_url(request, get_settings())
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            f"# llms.txt: {base_url}/llms.txt",
            "",
        ]
    )
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers=_discovery_headers(cache_seconds=3600),
    )


@router.get("/.well-known/mcp/server-card.json", tags=["MCP"])
async def mcp_server_card(request: Request) -> Response:
    """Static MCP server card for Smithery and other MCP registries."""
    tools = registry.to_mcp_server_card_tools()
    description = _mcp_server_description()

    # Include published marketplace tools
    s = get_settings()
    base_url = _public_base_url(request, s)
    if s.marketplace_enabled:
        try:
            mp_tools = await list_marketplace_tools()
            for mt in mp_tools:
                mt_entry: dict[str, Any] = {
                    "name": mt.name,
                    "title": mt.name.replace("_", " ").title(),
                    "description": mt.marketplace_description or mt.description,
                    "inputSchema": mt.input_schema,
                    "annotations": {"openWorldHint": True},
                }
                if mt.output_schema is not None:
                    mt_entry["outputSchema"] = mt.output_schema
                tools.append(mt_entry)
        except Exception:
            logger.debug("Failed to load marketplace tools for server card", exc_info=True)

    server_info: dict[str, Any] = {
        "name": "teardrop-tools",
        "title": "Teardrop",
        "description": description,
        "version": APP_VERSION,
        "websiteUrl": base_url,
    }
    content: dict[str, Any] = {
        "name": server_info["name"],
        "title": server_info["title"],
        "description": description,
        "homepage": base_url,
        "documentationUrl": f"{base_url}/docs",
        "serverInfo": server_info,
        "authentication": {"required": True, "schemes": ["bearer"]},
        "tools": tools,
        "resources": [],
        "prompts": [],
    }
    if s.agent_card_icon_url:
        server_info["icons"] = [{"src": s.agent_card_icon_url}]
        content["iconUrl"] = s.agent_card_icon_url

    return _json_discovery_response(request, content)
