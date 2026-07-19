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
import uuid

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from shared.request_ip import client_ip_from_request
from teardrop.auth import decode_access_token
from teardrop.config import get_settings
from teardrop.public_url import public_base_url

logger = logging.getLogger(__name__)

_MCP_PREFIX = "/tools/mcp"


class MCPPathNormalizer:
    """Normalize the bare MCP mount path without using BaseHTTPMiddleware."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == _MCP_PREFIX:
            scope = dict(scope)
            scope["path"] = f"{_MCP_PREFIX}/"
            scope["raw_path"] = f"{_MCP_PREFIX}/".encode("utf-8")
        await self.app(scope, receive, send)


def _jsonrpc_error(req_id: int | str | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _mcp_402_resource(request: Request) -> dict[str, str]:
    base_url = public_base_url(request, get_settings())
    return {
        "url": f"{base_url}/tools/mcp",
        "description": "MCP gateway tools/call execution endpoint.",
        "mimeType": "application/json",
    }


class MCPGatewayMiddleware(BaseHTTPMiddleware):
    """Auth + billing + x402 gateway for the MCP endpoint."""

    def __init__(self, app, *, mounted: bool = False):  # noqa: ANN001
        super().__init__(app)
        self._mounted = mounted

    @staticmethod
    def _smithery_events_list_response(req_id: int | str | None) -> JSONResponse:
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"events": []},
            }
        )

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        # FastMCP is mounted at /tools/mcp and serves its root at "/".
        # Normalizing the bare mount path avoids FastAPI falling through to
        # /tools/{tool_id} and returning a method-mismatch 405.
        if self._mounted:
            request.scope["path"] = "/"
            request.scope["raw_path"] = b"/"
        elif request.scope.get("path") == _MCP_PREFIX:
            request.scope["path"] = f"{_MCP_PREFIX}/"
            request.scope["raw_path"] = f"{_MCP_PREFIX}/".encode("utf-8")

        # Only intercept MCP requests.
        if not self._mounted and not request.url.path.startswith(_MCP_PREFIX):
            return await call_next(request)

        settings = get_settings()

        # ── Public Discovery Gate ──────────────────────────────────────────────────
        rpc_id: int | str | None = None
        discovery_response: JSONResponse | None = None
        is_public_discovery = False
        if request.method != "POST":
            is_public_discovery = True
        else:
            try:
                # Sniff JSON-RPC method safely; Starlette caches body in request._body
                body = await request.body()
                if body:
                    data = json.loads(body)
                    rpc_id = data.get("id")
                    method = data.get("method", "")
                    if method == "ai.smithery/events/list":
                        discovery_response = self._smithery_events_list_response(rpc_id)
                        is_public_discovery = True
                    # Gate only execution (tools/call). Handshakes/listing/notifications are public.
                    elif method != "tools/call":
                        is_public_discovery = True
                else:
                    is_public_discovery = True
            except Exception:
                is_public_discovery = True

        if is_public_discovery:
            # Lightweight per-IP limit for unauthenticated discovery endpoints
            ip = client_ip_from_request(request, trusted_proxy_count=settings.trusted_proxy_count)

            if ip:
                from teardrop.rate_limit import _check_rate_limit

                allowed, remaining, reset_at = await _check_rate_limit(f"mcp:ip:{ip}", 60)
                if not allowed:
                    return JSONResponse(
                        status_code=429,
                        content=_jsonrpc_error(None, -32029, "Anonymous rate limit exceeded"),
                        headers={
                            "X-RateLimit-Limit": "60",
                            "X-RateLimit-Remaining": str(remaining),
                            "X-RateLimit-Reset": str(reset_at),
                            "Retry-After": "60",
                        },
                    )

            request.state.mcp_org_id = None
            request.state.mcp_auth_method = ""
            if discovery_response is not None:
                return discovery_response
            return await call_next(request)

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
            execution_failed = await self._response_indicates_failure(response)
            response = await self._settle_billing(request, pending_debit, response, execution_failed=execution_failed)

        return response

    @staticmethod
    async def _response_indicates_failure(response: Response) -> bool:
        """Buffer the response body and check for JSON-RPC ``isError: true``.

        Returns True if the body is parseable JSON-RPC with an error result so
        the caller can skip the credit debit.  On any parse failure the
        function returns False (defaults to billing — preserves original
        behaviour for unfamiliar response shapes).

        Side-effect: drains and replaces ``response.body_iterator`` with a
        replay iterator so the caller can still stream the body to the
        client.  Safe for both ``Response`` and ``StreamingResponse``.
        """
        try:
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                chunks.append(chunk)
            body = b"".join(chunks)

            async def _replay():
                yield body

            response.body_iterator = _replay()  # type: ignore[attr-defined]

            if not body:
                return False
            data = json.loads(body)
            # JSON-RPC tools/call result is {"result": {"isError": true|false, ...}}
            result = data.get("result") if isinstance(data, dict) else None
            if isinstance(result, dict) and result.get("isError") is True:
                return True
        except Exception:
            return False
        return False

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

        from teardrop.rate_limit import _check_rate_limit  # lazy import — avoids circular dep at module level

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
        response_kwargs = {"resource": _mcp_402_resource(request)}
        if not payment_header:
            return JSONResponse(
                status_code=402,
                content=build_402_response_body(**response_kwargs),
                headers=build_402_headers(**response_kwargs),
            )

        billing = await verify_payment(payment_header)
        if not billing.verified:
            response_kwargs["error"] = billing.error
            return JSONResponse(
                status_code=402,
                content=build_402_response_body(**response_kwargs),
                headers=build_402_headers(**response_kwargs),
            )

        request.state.x402_billing = billing
        request.state.mcp_org_id = None
        request.state.mcp_auth_method = "x402"
        return None  # success — continue to billing / FastMCP

    async def _billing_gate(self, request: Request) -> tuple | Response | None:
        """Pre-request billing gate for ``tools/call`` requests.

        Resolves the per-call tool cost for both rails, then routes by auth
        method:

        * x402 callers — return the pending tuple so the post-response hook can
          settle on-chain via ``settle_payment``. No credit verification or
          subscription gate applies (access is payment-gated, not org-gated).
        * credit callers — enforce the marketplace subscription gate and verify
          the org's credit balance before allowing execution.

        Returns:
            None — billing disabled or not a billable ``tools/call`` request.
            tuple(org_id, tool_cost, tool_name, req_id) — ready for post-settle.
                ``org_id`` is None for anonymous x402 callers.
            Response — billing rejected (subscription or insufficient credits).
        """
        settings = get_settings()
        if not settings.mcp_billing_enabled:
            return None

        org_id: str | None = getattr(request.state, "mcp_org_id", None)
        is_x402 = getattr(request.state, "x402_billing", None) is not None

        # Anonymous, non-x402 callers can't be billed on either rail.
        if not is_x402 and org_id is None:
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

        from billing import (
            get_current_pricing,
            get_tool_pricing_overrides,
            is_promotional_credit,
            resolve_tool_cost,
            verify_credit,
        )

        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        tool_cost = await resolve_tool_cost(tool_name, overrides, default_cost, settings.marketplace_enabled)

        # x402 callers are billed via on-chain settlement after execution. The
        # subscription gate and credit verification are credit-rail concepts and
        # do not apply to anonymous per-call x402 payments.
        if is_x402:
            return (org_id, tool_cost, tool_name, req_id)

        # Verified-email promotional credit must not create author earnings
        # through a direct marketplace MCP call. Platform tools are not
        # author-owned and remain available on this rail.
        if (
            settings.onboarding_credit_enabled
            and "/" in tool_name
            and not tool_name.startswith("platform/")
            and await is_promotional_credit(org_id)
        ):
            logger.info("mcp promotional credit blocked marketplace tool org_id=%s tool=%s", org_id, tool_name)
            return JSONResponse(
                status_code=403,
                content=_jsonrpc_error(
                    req_id,
                    -32003,
                    "Marketplace author tools require a funded credit balance.",
                ),
            )

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

    @staticmethod
    async def _enqueue_mcp_recovery(org_id, tool_cost: int, billing_method: str, billing) -> None:
        """Enqueue a failed MCP settlement for asynchronous retry.

        The MCP gateway has no usage_event row, so a synthetic UUID anchors both
        the usage_event_id and run_id (``pending_settlements`` has no FK). Mirrors
        the recovery path in ``agent_post_run.dispatch_settlement``.
        """
        from billing.settlement import enqueue_failed_settlement

        payment_payload = None
        if billing is not None and getattr(billing, "payment_payload", None):
            payment_payload = str(billing.payment_payload)
        try:
            call_id = str(uuid.uuid4())
            await enqueue_failed_settlement(
                call_id,
                org_id or "",
                call_id,
                billing_method,
                tool_cost,
                payment_payload=payment_payload,
            )
        except Exception:
            logger.exception("Failed to enqueue MCP settlement recovery org=%s method=%s", org_id, billing_method)

    async def _settle_billing(
        self,
        request: Request,
        pending: tuple,
        response: Response,
        *,
        execution_failed: bool = False,
    ) -> Response:
        """Post-response: debit credits or settle x402.

        Returns the (potentially body-replayed) response.  When
        ``execution_failed`` is True we skip both the credit debit and the
        on-chain settlement — subscribers must not be charged for failed
        tool executions.
        """
        org_id, tool_cost, tool_name, _req_id = pending

        is_x402 = getattr(request.state, "x402_billing", None) is not None

        if execution_failed:
            logger.info("mcp settle skipped (execution failed) org=%s tool=%s", org_id, tool_name)
            return response

        if is_x402:
            # Phase 3: on-chain settlement.
            from billing import settle_payment

            billing = request.state.x402_billing
            try:
                settled = await settle_payment(billing, actual_cost_usdc=tool_cost)
            except Exception:
                logger.warning("x402 MCP settlement failed", exc_info=True)
                await self._enqueue_mcp_recovery(org_id, tool_cost, "x402", billing)
                return response  # Settlement failed — do not record phantom earnings

            if not settled.settled:
                logger.warning("x402 MCP settlement rejected org=%s tool=%s error=%s", org_id, tool_name, settled.error)
                await self._enqueue_mcp_recovery(org_id, tool_cost, "x402", billing)
                return response

            if settled.tx_hash:
                logger.info("x402 MCP settlement succeeded org=%s tool=%s tx_hash=%s", org_id, tool_name, settled.tx_hash)
        else:
            # Phase 2: credit debit.
            from billing import debit_credit

            debited, _ = await debit_credit(org_id, tool_cost, reason=f"mcp:{tool_name}")
            if not debited:
                logger.warning("MCP debit failed org=%s tool=%s", org_id, tool_name)
                await self._enqueue_mcp_recovery(org_id, tool_cost, "credit", None)
                return response

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

        try:
            from marketplace import record_marketplace_tool_usage_many

            asyncio.create_task(record_marketplace_tool_usage_many([tool_name]))
        except Exception:
            logger.debug("Failed to record MCP marketplace stats", exc_info=True)

        return response
