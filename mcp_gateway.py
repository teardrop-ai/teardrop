# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""ASGI middleware for the /tools/mcp endpoint.

Three layers, each behind a feature flag:
  Phase 1 – JWT auth gate  (mcp_auth_enabled)
  Phase 2 – Credit billing  (mcp_billing_enabled)
  Phase 3 – x402 on-chain   (mcp_x402_enabled)
"""

from __future__ import annotations

import asyncio
import json
import logging

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from auth import decode_access_token
from config import get_settings

logger = logging.getLogger(__name__)

_MCP_PREFIX = "/tools/mcp"


def _jsonrpc_error(req_id: int | str | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class MCPGatewayMiddleware(BaseHTTPMiddleware):
    """Auth + billing + x402 gateway for the MCP endpoint."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        # Only intercept MCP requests.
        if not request.url.path.startswith(_MCP_PREFIX):
            return await call_next(request)

        settings = get_settings()

        # ── Phase 1: JWT auth (or x402 fallback) ──────────────────────────
        auth_response = await self._authenticate(request, settings)
        if auth_response is not None:
            return auth_response

        # ── Phase 1.5: per-org aggregate rate limit ───────────────────────
        rate_limit_response = await self._enforce_org_rate_limit(request, settings)
        if rate_limit_response is not None:
            return rate_limit_response

        # ── Phase 2: credit billing gate ──────────────────────────────────
        pending_debit = await self._billing_gate(request)
        if isinstance(pending_debit, Response):
            return pending_debit

        # ── Forward to FastMCP ────────────────────────────────────────────
        response = await call_next(request)

        # ── Post-response: settle billing ─────────────────────────────────
        if response.status_code == 200 and pending_debit is not None:
            await self._settle_billing(request, pending_debit)

        return response

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _authenticate(self, request: Request, settings) -> Response | None:
        """Phase 1: JWT auth gate with optional x402 payment fallback.

        Sets ``request.state.mcp_org_id`` and ``request.state.mcp_auth_method``
        on success. Returns a Response on failure, or None to proceed.
        """
        token = self._extract_bearer(request)

        if token:
            try:
                payload = decode_access_token(token)
            except jwt.ExpiredSignatureError:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="token_expired"',
                    },
                )
            except jwt.InvalidTokenError:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="invalid_token"',
                    },
                )

            # Optional audience check.
            if settings.mcp_auth_audience and payload.get("aud") != settings.mcp_auth_audience:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="invalid_audience"',
                    },
                )

            request.state.mcp_org_id = payload.get("org_id", "")
            request.state.mcp_auth_method = payload.get("auth_method", "")
            return None

        if settings.mcp_auth_enabled:
            # No Bearer token and auth is required.
            # Phase 3: check for x402 payment header as fallback.
            if settings.mcp_x402_enabled:
                return await self._handle_x402_auth(request)
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="teardrop-mcp"'},
            )

        # Auth disabled — pass through with empty state.
        request.state.mcp_org_id = None
        request.state.mcp_auth_method = ""
        return None

    async def _enforce_org_rate_limit(self, request: Request, settings) -> Response | None:
        """Phase 1.5: per-org aggregate rate limit (skipped for x402/anonymous)."""
        mcp_org_id = getattr(request.state, "mcp_org_id", None)
        if not mcp_org_id:
            return None

        from app import _check_rate_limit  # lazy import — avoids circular dep at module level

        org_allowed, org_remaining, org_reset_at = await _check_rate_limit(
            f"mcp:org:{mcp_org_id}", settings.rate_limit_org_mcp_rpm
        )
        if org_allowed:
            return None

        return JSONResponse(
            status_code=429,
            content={
                "error": "Organization MCP rate limit exceeded. Please slow down.",
                "code": -32029,
            },
            headers={
                "X-RateLimit-Limit": str(settings.rate_limit_org_mcp_rpm),
                "X-RateLimit-Remaining": str(org_remaining),
                "X-RateLimit-Reset": str(org_reset_at),
                "Retry-After": "60",
                "X-RateLimit-Scope": "org",
            },
        )

    @staticmethod
    def _extract_bearer(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    async def _handle_x402_auth(self, request: Request) -> Response | None:
        """Handle x402 auth for unauthenticated callers.

        Returns a Response on failure (402), or None on success (sets state).
        """
        from billing import (
            build_402_headers,
            build_402_response_body,
            verify_payment,
        )

        payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
        if not payment_header:
            return JSONResponse(
                status_code=402,
                content=build_402_response_body(),
                headers=build_402_headers(),
            )

        billing = await verify_payment(payment_header)
        if not billing.verified:
            return JSONResponse(
                status_code=402,
                content={"error": billing.error},
                headers=build_402_headers(),
            )

        request.state.x402_billing = billing
        request.state.mcp_org_id = None
        request.state.mcp_auth_method = "x402"
        return None  # success — continue to billing / FastMCP

    async def _billing_gate(self, request: Request) -> tuple | Response | None:
        """Pre-request credit verification.

        Returns:
            None — billing disabled or not a tools/call request.
            tuple(org_id, tool_cost, tool_name, req_id) — verified, ready for post-debit.
            Response — billing rejected (insufficient credits).
        """
        settings = get_settings()
        if not settings.mcp_billing_enabled:
            return None

        org_id: str | None = getattr(request.state, "mcp_org_id", None)
        is_x402 = getattr(request.state, "x402_billing", None) is not None

        # x402 callers are billed via on-chain settlement, not credits.
        # Anonymous callers with no org can't be credit-billed.
        if is_x402 or org_id is None:
            return None

        if request.method != "POST":
            return None

        # Read and cache body (Starlette will re-serve from _body).
        body = await request.body()

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        method = data.get("method", "")
        if method != "tools/call":
            return None

        params = data.get("params", {})
        tool_name: str = params.get("name", "")
        req_id = data.get("id")

        if not tool_name:
            return None

        from billing import get_current_pricing, get_tool_pricing_overrides, verify_credit

        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        # Marketplace tool split.
        actual_tool_name = tool_name.split("/", 1)[-1] if "/" in tool_name else tool_name
        tool_cost = overrides.get(actual_tool_name, default_cost)

        # ── Platform tool pricing ─────────────────────────────────────────
        # Platform tools (tools/definitions/) are not qualified with "/".
        # Look up their marketplace price from marketplace_platform_tools.
        if "/" not in tool_name and settings.marketplace_enabled:
            from marketplace import get_platform_tool_price

            platform_price = await get_platform_tool_price(tool_name)
            if platform_price is not None:
                tool_cost = overrides.get(tool_name, platform_price)

        # Subscription gate: marketplace tools require an active subscription.
        if "/" in tool_name and settings.marketplace_enabled:
            from marketplace import check_org_subscription

            if not await check_org_subscription(org_id, tool_name):
                logger.info("mcp subscription check failed org_id=%s tool=%s", org_id, tool_name)
                return JSONResponse(
                    status_code=403,
                    content=_jsonrpc_error(
                        req_id,
                        -32001,
                        f"Not subscribed to marketplace tool '{tool_name}'. Subscribe via POST /marketplace/subscriptions.",
                    ),
                )

        billing = await verify_credit(org_id, tool_cost)
        if not billing.verified:
            return JSONResponse(
                status_code=402,
                content=_jsonrpc_error(req_id, -32000, billing.error),
            )

        return (org_id, tool_cost, tool_name, req_id)

    async def _settle_billing(
        self,
        request: Request,
        pending: tuple,
    ) -> None:
        """Post-response: debit credits or settle x402."""
        org_id, tool_cost, tool_name, _req_id = pending

        is_x402 = getattr(request.state, "x402_billing", None) is not None

        if is_x402:
            # Phase 3: on-chain settlement.
            from billing import settle_payment

            billing = request.state.x402_billing
            try:
                await settle_payment(billing, actual_cost_usdc=tool_cost)
            except Exception:
                logger.warning("x402 MCP settlement failed", exc_info=True)
                return  # Settlement failed — do not record phantom earnings
        else:
            # Phase 2: credit debit.
            from billing import debit_credit

            debited = await debit_credit(org_id, tool_cost, reason=f"mcp:{tool_name}")
            if not debited:
                logger.warning("MCP debit failed org=%s tool=%s", org_id, tool_name)
                return

        # Record marketplace earnings (fire-and-forget).
        if "/" in tool_name and tool_cost > 0:
            try:
                from marketplace import get_marketplace_tool_by_name, record_tool_call_earnings

                tool_org_slug, actual_name = tool_name.split("/", 1)
                tool_row = await get_marketplace_tool_by_name(actual_name, tool_org_slug)
                if tool_row is not None:
                    author_org_id = tool_row.get("org_id")
                    if author_org_id:
                        asyncio.create_task(
                            record_tool_call_earnings(
                                author_org_id=author_org_id,
                                caller_org_id=org_id or "",
                                tool_name=actual_name,
                                total_cost_usdc=tool_cost,
                            )
                        )
            except Exception:
                logger.debug("Failed to record MCP author earnings", exc_info=True)
