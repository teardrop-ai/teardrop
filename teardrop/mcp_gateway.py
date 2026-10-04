# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""ASGI middleware for the /tools/mcp endpoint.

Three layers, each behind a feature flag:
  Phase 1 – JWT auth gate  (mcp_auth_enabled)
  Phase 2 – Credit billing  (mcp_billing_enabled)
  Phase 3 – x402 on-chain   (mcp_x402_enabled)

Owner map:
  - module helpers:       x402 Bazaar discovery schema, 402 resource, JSON-RPC errors, _meta payment encoding
  - MCPPathNormalizer:    bare-mount path rewrite (pure ASGI)
  - MCPGatewayMiddleware:
      dispatch                          public-discovery gate → auth → billing gate → call → settle
      _authenticate / _enforce_org_rate_limit   JWT auth + per-org rate limit
      _handle_x402_auth / _x402_challenge       x402 payment verification + 402 challenge
      _billing_gate / _settle_billing           credit/x402 pre-check and post-call settlement
      _record_mcp_outcome / _enqueue_mcp_recovery   telemetry + settlement retry enqueue
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from datetime import datetime, timezone

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from x402.extensions.bazaar import (
    DeclareMcpDiscoveryConfig,
    OutputConfig,
    declare_discovery_extension,
    declare_mcp_discovery_extension,
)

from shared.request_ip import client_ip_from_request
from teardrop.auth import decode_access_token, is_machine_credential_revoked
from teardrop.config import get_settings
from teardrop.public_url import public_base_url
from tools.registry import MCP_MAX_COST_META_KEY
from tools.schema import flatten_embedded_json_schema

logger = logging.getLogger(__name__)

_MCP_PREFIX = "/tools/mcp"
# Build order §5.2: unbilled failures per IP/payer before anonymous execution stays free — 3 per 10 min, then 429.
_FAILURE_BUDGET_LIMIT = 3
_FAILURE_BUDGET_WINDOW_SECONDS = 600
# Mirrors billing.mpp (spec: mpp.dev/protocol/transports/mcp) so gateway startup never imports billing here.
_MPP_CREDENTIAL_META_KEY = "org.paymentauth/credential"
_MPP_RECEIPT_META_KEY = "org.paymentauth/receipt"
_MPP_CHALLENGES_META_KEY = "org.paymentauth/challenges"
_MPP_VERIFY_FAILED_CODE = -32043
_MPP_MALFORMED_CODE = -32602
# Must be a paid tool: the Bazaar replays this example and expects a 402 (never an _ANON_FREE_TOOLS entry).
_MCP_BAZAAR_INPUT_EXAMPLE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "get_token_price", "arguments": {"tokens": ["ETH"]}},
}
_MCP_BAZAAR_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "jsonrpc": {"type": "string", "const": "2.0"},
        "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        "method": {"type": "string", "const": "tools/call"},
        "params": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "arguments": {"type": "object"},
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        },
    },
    "required": ["jsonrpc", "id", "method", "params"],
    "additionalProperties": False,
}
_MCP_BAZAAR_OUTPUT_EXAMPLE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [
            {
                "type": "text",
                "text": '{"vs_currency":"usd","prices":[{"id":"ethereum","symbol":"eth","price":3500.0,'
                '"market_cap":null,"volume_24h":null,"change_24h_pct":null}]}',
            }
        ],
        "isError": False,
    },
}
# Mirrors x402.mcp.types so gateway startup never depends on importing x402.mcp.
_MCP_PAYMENT_META_KEY = "x402/payment"
_MCP_PAYMENT_RESPONSE_META_KEY = "x402/payment-response"
_MCP_PAYMENT_HINT = (
    "Payment required. Pay per call with x402 by retrying with params._meta['x402/payment'] "
    "built from structuredContent.accepts, or send 'Authorization: Bearer <token>' from POST /token "
    "(grant_type=x402 bootstraps an org) to use prepaid credits."
)
# Price lookups fail open to 0, so a zero cost alone is not proof a tool is free.
_ANON_FREE_TOOLS = frozenset({"calculate", "get_datetime", "count_text_stats", "discover_agents"})
_ANON_IP_LIMIT_PER_MINUTE = 60
_JSONRPC_MESSAGE_KEYS = frozenset({"method", "result", "error"})
# The CDP facilitator rejects verify and settle when a discovery description exceeds 500 characters.
_BAZAAR_DESCRIPTION_MAX_CHARS = 500


def _bazaar_description(text: str) -> str:
    if len(text) <= _BAZAAR_DESCRIPTION_MAX_CHARS:
        return text
    return text[: _BAZAAR_DESCRIPTION_MAX_CHARS - 1].rstrip() + "\u2026"


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


async def _anonymous_ip_limit(request: Request) -> JSONResponse | None:
    """Per-IP limit shared by anonymous discovery and free tool calls."""
    ip = client_ip_from_request(request, trusted_proxy_count=get_settings().trusted_proxy_count)
    if not ip:
        return None
    from teardrop.rate_limit import _check_rate_limit

    allowed, remaining, reset_at = await _check_rate_limit(f"mcp:ip:{ip}", _ANON_IP_LIMIT_PER_MINUTE)
    if allowed:
        return None
    return JSONResponse(
        status_code=429,
        content=_jsonrpc_error(None, -32029, "Anonymous rate limit exceeded"),
        headers={
            "X-RateLimit-Limit": str(_ANON_IP_LIMIT_PER_MINUTE),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_at),
            "Retry-After": "60",
        },
    )


async def _anonymous_failure_budget(request: Request, req_id: int | str | None) -> JSONResponse | None:
    """429 once this IP exhausted its unbilled-failure budget (build order §3.1/§5.2)."""
    from teardrop.rate_limit import check_auth_lockout

    ip = client_ip_from_request(request, trusted_proxy_count=get_settings().trusted_proxy_count)
    if not ip:
        return None
    locked, retry_after = await check_auth_lockout(f"mcpfail:ip:{ip}", _FAILURE_BUDGET_LIMIT, _FAILURE_BUDGET_WINDOW_SECONDS)
    if not locked:
        return None
    return JSONResponse(
        status_code=429,
        content=_jsonrpc_error(req_id, -32029, "Anonymous tool failure budget exceeded"),
        headers={"X-RateLimit-Scope": "failure-budget", "Retry-After": str(max(1, retry_after))},
    )


async def _payer_failure_budget(payer: str, req_id: int | str | None) -> JSONResponse | None:
    """429 once a verified x402 payer exhausted its unbilled-failure budget."""
    from teardrop.rate_limit import check_auth_lockout

    subject = payer.strip().lower()
    if not subject:
        return None
    locked, retry_after = await check_auth_lockout(
        f"mcpfail:payer:{subject}", _FAILURE_BUDGET_LIMIT, _FAILURE_BUDGET_WINDOW_SECONDS
    )
    if not locked:
        return None
    return JSONResponse(
        status_code=429,
        content=_jsonrpc_error(req_id, -32029, "Payer tool failure budget exceeded"),
        headers={"X-RateLimit-Scope": "x402-payer", "Retry-After": str(max(1, retry_after))},
    )


async def _record_unbilled_failure(request: Request) -> None:
    """Count one unbilled execution failure against the caller's IP and verified payer.

    Hooked where the tool ran but nobody was charged (§5.2). Never raises:
    budget accounting must not disturb the response path.
    """
    from teardrop.rate_limit import record_auth_failure

    try:
        billing = getattr(request.state, "x402_billing", None)
        if billing is not None and getattr(billing, "billing_method", "x402") == "mpp":
            return  # billed on-chain before execution — not an unbilled failure
        ip = client_ip_from_request(request, trusted_proxy_count=get_settings().trusted_proxy_count)
        if ip:
            await record_auth_failure(f"mcpfail:ip:{ip}", _FAILURE_BUDGET_WINDOW_SECONDS)
        billing = getattr(request.state, "x402_billing", None)
        payer = str(getattr(billing, "payer", "") or "").strip().lower()
        if payer:
            await record_auth_failure(f"mcpfail:payer:{payer}", _FAILURE_BUDGET_WINDOW_SECONDS)
    except Exception:
        logger.debug("failure-budget accounting skipped", exc_info=True)


def _mcp_402_resource(request: Request) -> dict:
    base_url = public_base_url(request, get_settings())
    return {
        "url": f"{base_url}/tools/mcp",
        "description": "MCP gateway tools/call execution endpoint.",
        "mimeType": "application/json",
    }


def _tool_call_name(data: dict) -> str | None:
    params = data.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) and name else None


def _is_community_tool(name: str | None) -> bool:
    return isinstance(name, str) and "/" in name and not name.startswith("platform/")


def _x402_payable(name: str | None) -> bool:
    # Community tools stay credit-only until anonymous payers get earnings attribution (caller_org_id).
    return not _is_community_tool(name)


def _bearer_required(req_id: int | str | None) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content=_jsonrpc_error(
            req_id,
            -32001,
            "Marketplace tools require 'Authorization: Bearer <token>' from POST /token and funded credit.",
        ),
        headers={"WWW-Authenticate": 'Bearer realm="teardrop-mcp"'},
    )


def _mcp_402_extensions(tool_name: str | None = None) -> dict:
    from tools import registry

    tool = registry.get(tool_name) if tool_name else None
    if tool is not None:
        # Bazaar keys MCP listings on (resource, toolName), so each tool gets its own entry.
        return declare_mcp_discovery_extension(
            DeclareMcpDiscoveryConfig(
                tool_name=tool.name,
                description=_bazaar_description(tool.description),
                transport="streamable-http",
                input_schema=flatten_embedded_json_schema(tool.input_schema.model_json_schema()),
            )
        )
    extension = declare_discovery_extension(
        input=_MCP_BAZAAR_INPUT_EXAMPLE,
        input_schema=_MCP_BAZAAR_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(example=_MCP_BAZAAR_OUTPUT_EXAMPLE),
    )
    extension["bazaar"]["info"]["input"]["method"] = "POST"
    return extension


def _wants_mcp_payment_signal(request: Request) -> bool:
    # Streamable-HTTP MCP clients must accept SSE; plain HTTP x402 clients expect a bare 402.
    return "mcp-protocol-version" in request.headers or "text/event-stream" in request.headers.get("accept", "").lower()


async def _read_jsonrpc(request: Request) -> dict:
    try:
        data = json.loads(await request.body())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _meta_payment_header(data: dict) -> str | None:
    """Encode an MCP ``_meta`` x402 payment as the base64 header form ``verify_payment`` expects."""
    params = data.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    payment = meta.get(_MCP_PAYMENT_META_KEY) if isinstance(meta, dict) else None
    if payment is None:
        return None
    if isinstance(payment, str):
        try:
            payment = json.loads(payment)
        except ValueError:
            return base64.b64encode(payment.encode("utf-8")).decode("ascii")
    # Canonical form keeps the replay-nonce hash stable across key order and whitespace.
    canonical = json.dumps(payment, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(canonical.encode("utf-8")).decode("ascii")


def _mpp_credential(request: Request, data: dict) -> object | None:
    """MPP credential from ``params._meta`` (MCP transport) or ``Authorization: Payment`` (HTTP transport).

    None routes the call to the x402 path.
    """
    params = data.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    credential = meta.get(_MPP_CREDENTIAL_META_KEY) if isinstance(meta, dict) else None
    if credential is not None:
        return credential
    from billing.mpp import parse_payment_authorization

    return parse_payment_authorization(request.headers.get("authorization"))


def _mpp_error(
    req_id: int | str | None, code: int, message: str, data: dict | None = None, challenge: dict | None = None
) -> JSONResponse:
    """Spec-shaped MPP payment error: JSON-RPC error, HTTP 402 carrying ``error.data``.

    Per the MCP transport spec the challenge rides in ``error.data.challenges``;
    plain HTTP clients also get it as ``WWW-Authenticate: Payment``.
    """
    error = _jsonrpc_error(req_id, code, message)
    if data is not None:
        error["error"]["data"] = data
    headers = None
    if challenge is not None:
        from billing.mpp import mpp_www_authenticate

        headers = {"WWW-Authenticate": mpp_www_authenticate(challenge)}
    return JSONResponse(status_code=402, content=error, headers=headers)


def _mcp_payment_required(req_id: int | str | None, payment_required: dict, mpp_challenge: dict | None = None) -> JSONResponse:
    result: dict = {
        "content": [
            {"type": "text", "text": json.dumps(payment_required)},
            {"type": "text", "text": _MCP_PAYMENT_HINT},
        ],
        "structuredContent": payment_required,
        "isError": True,
    }
    if mpp_challenge is not None:
        # x402 stays the primary MCP challenge; MPP clients find their offer in _meta.
        result["_meta"] = {_MPP_CHALLENGES_META_KEY: [mpp_challenge]}
    return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})


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
        # MCPServer is mounted at /tools/mcp and serves its root at "/".
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
        is_payment_probe = False
        if request.method != "POST":
            is_public_discovery = True
        else:
            try:
                # Sniff JSON-RPC method safely; Starlette caches body in request._body
                body = await request.body()
                data = json.loads(body) if body.strip() else {}
                rpc_id = data.get("id")
                method = data.get("method", "")
                if method:
                    # 2026-07-28 envelope share (incl. tools/call) — feeds the modern-client tripwire.
                    from teardrop.funnel_counters import (
                        MCP_PROTOCOL_VERSION_META_KEY,
                        SURFACE_MCP_MODERN_ENVELOPE,
                        SURFACE_MCP_REQUEST,
                        mcp_call_client_surface,
                        mcp_meta_client_name,
                        record_discovery_hit,
                    )

                    record_discovery_hit(SURFACE_MCP_REQUEST)
                    sniff_params = data.get("params")
                    sniff_meta = sniff_params.get("_meta") if isinstance(sniff_params, dict) else None
                    if isinstance(sniff_meta, dict) and MCP_PROTOCOL_VERSION_META_KEY in sniff_meta:
                        record_discovery_hit(SURFACE_MCP_MODERN_ENVELOPE)
                    if method == "tools/call":
                        # Stateless clients may never handshake; attribute each call by its _meta clientInfo.
                        call_client = mcp_meta_client_name(sniff_params)
                        if call_client is not None:
                            record_discovery_hit(mcp_call_client_surface(call_client))
                if method == "ai.smithery/events/list":
                    discovery_response = self._smithery_events_list_response(rpc_id)
                    is_public_discovery = True
                elif (
                    not (_JSONRPC_MESSAGE_KEYS & data.keys())
                    and settings.mcp_auth_enabled
                    and settings.mcp_x402_enabled
                    and self._extract_bearer(request) is None
                ):
                    # A non-JSON-RPC POST (e.g. the Bazaar validator's empty probe) gets the paid resource's 402.
                    is_payment_probe = True
                # Gate only execution (tools/call). Handshakes/listing/notifications are public.
                elif method != "tools/call":
                    is_public_discovery = True
            except Exception:
                is_public_discovery = True

        if is_payment_probe:
            limited = await _anonymous_ip_limit(request)
            if limited is not None:
                return limited
            return await self._payment_probe_challenge(request)

        if is_public_discovery:
            limited = await _anonymous_ip_limit(request)
            if limited is not None:
                return limited

            from teardrop.funnel_counters import (
                SURFACE_TOOLS_LIST,
                SURFACE_TOOLS_LIST_ANON,
                mcp_initialize_surface,
                mcp_meta_client_name,
                record_discovery_hit,
            )

            try:
                rpc = json.loads(body or b"{}")
            except Exception:
                rpc = None
            rpc_method = rpc.get("method") if isinstance(rpc, dict) else None
            is_tools_list = rpc_method == "tools/list"
            is_anonymous = self._extract_bearer(request) is None
            if is_tools_list:
                record_discovery_hit(SURFACE_TOOLS_LIST)
                if is_anonymous:
                    record_discovery_hit(SURFACE_TOOLS_LIST_ANON)
            elif rpc_method in ("initialize", "server/discover"):
                # Modern clients replace initialize with server/discover and put clientInfo in params._meta.
                params = rpc.get("params")
                client_info = params.get("clientInfo") if isinstance(params, dict) else None
                client_name = client_info.get("name") if isinstance(client_info, dict) else None
                if client_name is None:
                    client_name = mcp_meta_client_name(params)
                record_discovery_hit(mcp_initialize_surface(client_name))

            request.state.mcp_org_id = None
            request.state.mcp_auth_method = ""
            if discovery_response is not None:
                return discovery_response
            response = await call_next(request)
            if is_tools_list and is_anonymous:
                # Community tools are Bearer + credit only, so anonymous (x402) callers never see them.
                return await self._without_community_tools(response)
            return response

        # ── Phase 0: anonymous unbilled-failure budget (build order §3.1) ──
        # MPP credentials are prepaid on-chain; a 429 here could outlast the Challenge and strand
        # the payment, so their budget check runs inside _handle_x402_auth only for unpaid paths.
        if method == "tools/call" and self._is_anonymous(request) and not self._carries_mpp_credential(request, data):
            budget_response = await _anonymous_failure_budget(request, rpc_id)
            if budget_response is not None:
                return budget_response

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
        if pending_debit is None and getattr(request.state, "x402_billing", None) is not None:
            # A verified x402 payment must never execute without a settlement path.
            logger.error("x402 MCP payment verified but billing gate is inactive; rejecting call")
            await self._release_payment_claim(request)
            return JSONResponse(
                status_code=503,
                content=_jsonrpc_error(rpc_id, -32603, "Paid MCP execution is temporarily unavailable."),
            )
        if pending_debit is None and _is_community_tool(_tool_call_name(await _read_jsonrpc(request))):
            # Community tools never run unbilled, whatever the auth/billing flags say.
            if not settings.mcp_billing_enabled:
                return JSONResponse(
                    status_code=503,
                    content=_jsonrpc_error(rpc_id, -32603, "Paid MCP execution is temporarily unavailable."),
                )
            return _bearer_required(rpc_id)

        # ── Forward to MCPServer ──────────────────────────────────────────
        try:
            response = await call_next(request)
        except Exception:
            await self._release_x402_reservation(request)
            raise

        # ── Post-response: settle billing ─────────────────────────────────
        if response.status_code == 200 and pending_debit is not None:
            execution_failed = await self._response_indicates_failure(response)
            response = await self._settle_billing(request, pending_debit, response, execution_failed=execution_failed)
        elif pending_debit is not None:
            await self._release_x402_reservation(request)
        elif response.status_code == 200 and self._is_anonymous(request):
            # Phase 0 population with no settlement path (free/allowlisted tools,
            # billing disabled): count failed executions against the IP budget.
            # Settle-path failures never reach this branch (pending_debit is not
            # None above), so nothing here double-counts what _settle_billing
            # already recorded; JWT-authenticated callers are not anonymous.
            if await self._response_indicates_failure(response):
                await _record_unbilled_failure(request)

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
            # json_response mode returns JSON-RPC errors (e.g. invalid params) as HTTP 200.
            if isinstance(data, dict) and "error" in data:
                return True
            # JSON-RPC tools/call result is {"result": {"isError": true|false, ...}}
            result = data.get("result") if isinstance(data, dict) else None
            if isinstance(result, dict) and result.get("isError") is True:
                return True
        except Exception:
            return False
        return False

    @staticmethod
    async def _without_community_tools(response: Response) -> Response:
        if response.status_code != 200:
            return response
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        body = b"".join(chunks)
        try:
            data = json.loads(body)
            data["result"]["tools"] = [tool for tool in data["result"]["tools"] if not _is_community_tool(tool.get("name"))]
            body = json.dumps(data).encode("utf-8")
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.debug("MCP tools/list response left unfiltered", exc_info=True)
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        return Response(content=body, status_code=response.status_code, headers=headers)

    @staticmethod
    async def _attach_mpp_receipt(response: Response, request: Request) -> Response:
        """Add the spec receipt as ``result._meta["org.paymentauth/receipt"]`` and ``Payment-Receipt``."""
        from billing.mpp import encode_receipt

        chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        body = b"".join(chunks)
        receipt = {
            "status": "success",
            "method": "evm",
            "reference": request.state.x402_billing.tx_hash,
            "challengeId": getattr(request.state, "mcp_mpp_challenge_id", ""),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            parsed = json.loads(body)
            result = parsed["result"]
            meta = result.get("_meta")
            result["_meta"] = {**(meta if isinstance(meta, dict) else {}), _MPP_RECEIPT_META_KEY: receipt}
            body = json.dumps(parsed).encode("utf-8")
        except Exception:
            # The payment already confirmed; a missing _meta receipt must not fail the call.
            logger.debug("MPP receipt not attached to result", exc_info=True)
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        headers["Payment-Receipt"] = encode_receipt(receipt)
        return Response(content=body, status_code=response.status_code, headers=headers)

    @staticmethod
    async def _attach_payment_receipt(response: Response, billing) -> Response:  # noqa: ANN001
        """Add the x402 MCP ``x402/payment-response`` receipt to a settled tools/call result."""
        from x402.schemas import SettleResponse

        chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        body = b"".join(chunks)
        try:
            receipt = SettleResponse(
                success=True,
                transaction=billing.tx_hash or "",
                network=getattr(billing.payment_requirements, "network", ""),
                payer=billing.payer or None,
                amount=str(billing.amount_usdc),
            ).model_dump(by_alias=True, exclude_none=True)
            data = json.loads(body)
            result = data["result"]
            meta = result.get("_meta")
            result["_meta"] = {**(meta if isinstance(meta, dict) else {}), _MCP_PAYMENT_RESPONSE_META_KEY: receipt}
            body = json.dumps(data).encode("utf-8")
        except Exception:
            # Settlement already succeeded; a missing receipt must not fail the call.
            logger.debug("x402 MCP payment receipt not attached", exc_info=True)
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        return Response(content=body, status_code=response.status_code, headers=headers)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _authenticate(self, request: Request, settings) -> Response | None:
        """Phase 1: JWT auth gate with optional x402 payment fallback.

        Sets ``request.state.mcp_org_id`` and ``request.state.mcp_auth_method``
        on success. Returns a Response on failure, or None to proceed.
        """
        token = self._extract_bearer(request)

        if token:
            try:
                payload = decode_access_token(token, audience=settings.mcp_auth_audience or None)
            except jwt.ExpiredSignatureError:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="token_expired"',
                    },
                )
            except jwt.InvalidAudienceError:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="invalid_audience"',
                    },
                )
            except jwt.InvalidTokenError:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="invalid_token"',
                    },
                )

            if await is_machine_credential_revoked(payload):
                return Response(
                    status_code=403,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="teardrop-mcp", error="credential_disabled"',
                    },
                )

            request.state.mcp_org_id = payload.get("org_id", "")
            request.state.mcp_auth_method = payload.get("auth_method", "")
            request.state.mcp_principal_id = payload.get("sub", "")
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

    @staticmethod
    def _is_anonymous(request: Request) -> bool:
        """True when the request carries no usable Bearer credential.

        Shares ``_extract_bearer``'s truthiness semantics with
        ``_authenticate`` so an empty ``Bearer `` header counts as anonymous
        for both the Phase 0 gate and the unbilled-failure recorder.
        """
        return not MCPGatewayMiddleware._extract_bearer(request)

    @staticmethod
    def _meta_payment(request: Request) -> str | None:
        payment = getattr(request.state, "mcp_x402_payment", None)
        return payment if isinstance(payment, str) and payment else None

    @staticmethod
    def _payment_header(request: Request) -> str | None:
        return (
            request.headers.get("payment-signature")
            or request.headers.get("x-payment")
            or MCPGatewayMiddleware._meta_payment(request)
        )

    @staticmethod
    async def _release_x402_reservation(request: Request) -> None:
        billing = getattr(request.state, "x402_billing", None)
        if billing is not None and getattr(billing, "billing_method", "x402") == "mpp":
            # Never free an mpp tx-hash claim: releasing would let the same
            # on-chain payment replay (expiry bounds it; the claim closes it).
            request.state.mcp_x402_reserved = False
            return
        if getattr(request.state, "mcp_x402_reserved", False) is not True:
            return
        payment_header = MCPGatewayMiddleware._payment_header(request)
        if payment_header:
            from billing import release_payment_nonce

            try:
                await release_payment_nonce(payment_header)
            except Exception:
                logger.warning("x402 MCP reservation release failed", exc_info=True)
        request.state.mcp_x402_reserved = False

    @staticmethod
    async def _release_payment_claim(request: Request) -> None:
        """Release the x402 replay nonce — never an MPP claim.

        ``release_payment_nonce`` is scheme-blind: on an ``mpp:<tx>`` key it
        would delete the durable replay claim and let a consumed on-chain
        payment execute again, so every rejection path routes through here.
        """
        billing = getattr(request.state, "x402_billing", None)
        if billing is not None and getattr(billing, "billing_method", "x402") == "mpp":
            return
        payment_header = MCPGatewayMiddleware._payment_header(request)
        if not payment_header:
            return
        from billing import release_payment_nonce

        await release_payment_nonce(payment_header)

    async def _handle_x402_auth(self, request: Request) -> Response | None:
        """Handle x402 auth for unauthenticated callers.

        Accepts payment from HTTP headers or the x402 MCP transport's
        ``params._meta["x402/payment"]``. Returns a payment-required Response on
        failure, or None on success (sets state).
        """
        from billing import verify_payment
        from teardrop.funnel_counters import (
            SURFACE_MCP_402_CHALLENGE,
            SURFACE_MCP_402_NO_PAYMENT,
            SURFACE_MCP_402_PAYMENT_INVALID,
            anon_challenge_client_surface,
            record_discovery_hit,
        )

        data = await _read_jsonrpc(request)
        tool_name = _tool_call_name(data)
        if not _x402_payable(tool_name):
            return _bearer_required(data.get("id"))
        # Phase 0 skipped MPP-bearing calls; re-apply the IP failure budget wherever they end up unpaid.
        mpp_bypassed_budget = self._carries_mpp_credential(request, data)
        if tool_name in _ANON_FREE_TOOLS:
            try:
                is_free = await self._resolve_tool_cost(tool_name) == 0
            except Exception:
                logger.warning("x402 MCP tool pricing unavailable", exc_info=True)
                return JSONResponse(
                    status_code=503,
                    content=_jsonrpc_error(data.get("id"), -32603, "Paid MCP pricing is temporarily unavailable."),
                )
            if is_free:
                if mpp_bypassed_budget:
                    budget_response = await _anonymous_failure_budget(request, data.get("id"))
                    if budget_response is not None:
                        return budget_response
                limited = await _anonymous_ip_limit(request)
                if limited is not None:
                    return limited
                request.state.mcp_org_id = None
                request.state.mcp_auth_method = ""
                return None

        # PaymentScheme seam: an MPP charge credential routes to the MPP verifier;
        # absent credential (or MPP unconfigured) keeps the x402 rail byte-for-byte.
        if mpp_bypassed_budget:
            mpp_response = await self._handle_mpp_auth(request, data)
            if mpp_response is not None:
                return mpp_response
            if getattr(getattr(request.state, "x402_billing", None), "billing_method", "") == "mpp":
                # Verified and claimed: dispatch with MPP state intact. Falling through would hand
                # the call to verify_payment and reject a payment already final on-chain.
                return None
            # None → zero-priced tool; continue on x402 (flat-requirement challenge).
            budget_response = await _anonymous_failure_budget(request, data.get("id"))
            if budget_response is not None:
                return budget_response

        payment_header = self._payment_header(request)
        if not payment_header:
            payment_header = _meta_payment_header(data)
            if payment_header:
                request.state.mcp_x402_payment = payment_header
        mcp_signal = self._meta_payment(request) is not None or _wants_mcp_payment_signal(request)
        try:
            requirements = await self._x402_tool_requirements(data)
        except Exception:
            logger.warning("x402 MCP tool pricing unavailable", exc_info=True)
            return JSONResponse(
                status_code=503,
                content=_jsonrpc_error(data.get("id"), -32603, "Paid MCP pricing is temporarily unavailable."),
            )
        response_kwargs = {
            "resource": _mcp_402_resource(request),
            "extensions": _mcp_402_extensions(_tool_call_name(data)),
        }
        if requirements is not None:
            response_kwargs["requirements"] = requirements
        client_surface = anon_challenge_client_surface(request.headers.get("user-agent"), mcp_signal)
        if not payment_header:
            record_discovery_hit(SURFACE_MCP_402_CHALLENGE)
            record_discovery_hit(SURFACE_MCP_402_NO_PAYMENT)
            record_discovery_hit(client_surface)
            return self._x402_challenge(data.get("id"), mcp_signal, response_kwargs, await self._mpp_offer(_tool_call_name(data)))

        billing = await verify_payment(payment_header, requirements)
        if not billing.verified:
            response_kwargs["error"] = billing.error
            record_discovery_hit(SURFACE_MCP_402_CHALLENGE)
            record_discovery_hit(SURFACE_MCP_402_PAYMENT_INVALID)
            record_discovery_hit(client_surface)
            return self._x402_challenge(data.get("id"), mcp_signal, response_kwargs)

        request.state.x402_billing = billing
        request.state.mcp_x402_challenge = (mcp_signal, response_kwargs)
        request.state.mcp_org_id = None
        request.state.mcp_auth_method = "x402"
        return None  # success — continue to billing / MCPServer

    @staticmethod
    def _carries_mpp_credential(request: Request, data: dict) -> bool:
        from billing.mpp import mpp_configured

        return mpp_configured(get_settings()) and _mpp_credential(request, data) is not None

    async def _mpp_offer(self, tool_name: str | None) -> dict | None:
        """MPP Challenge advertised next to the x402 challenge; None when MPP is off or the tool is free."""
        from billing.mpp import build_mpp_challenge, mpp_configured

        settings = get_settings()
        if tool_name is None or not mpp_configured(settings):
            return None
        try:
            cost = await self._resolve_tool_cost(tool_name)
        except Exception:
            # x402 already priced this call; a pricing blip only drops the optional MPP offer.
            logger.warning("MPP offer pricing unavailable", exc_info=True)
            return None
        return build_mpp_challenge(tool_cost_usdc=cost, settings=settings) if cost > 0 else None

    async def _handle_mpp_auth(self, request: Request, data: dict) -> Response | None:
        """Verify an MPP evm-charge ``hash`` credential (draft-evm-charge-00 §6.4).

        The transfer is final before execution, so there is no post-response
        settlement: success stashes a ``BillingResult`` with
        ``billing_method="mpp"`` for the billing gate and receipt attachment.
        Error mapping: malformed → ``-32602``; binding/expiry/replay/receipt
        mismatch → ``-32043`` with a replacement Challenge; store/RPC outage → 503.
        Returns None only for zero-priced tools (caller continues on x402).
        """
        from billing.mpp import build_mpp_challenge, verify_mpp_payment

        settings = get_settings()
        req_id = data.get("id")
        tool_name = _tool_call_name(data)
        try:
            tool_cost = await self._resolve_tool_cost(tool_name) if tool_name is not None else 0
        except Exception:
            logger.warning("MPP MCP tool pricing unavailable", exc_info=True)
            return JSONResponse(
                status_code=503,
                content=_jsonrpc_error(req_id, -32603, "Paid MCP pricing is temporarily unavailable."),
            )
        if tool_cost <= 0:
            return None

        credential = _mpp_credential(request, data)
        outcome = await verify_mpp_payment(credential, tool_cost_usdc=tool_cost, settings=settings)
        if outcome.status in ("malformed", "failed"):
            replacement = build_mpp_challenge(tool_cost_usdc=tool_cost, settings=settings)
            code = _MPP_MALFORMED_CODE if outcome.status == "malformed" else _MPP_VERIFY_FAILED_CODE
            return _mpp_error(
                req_id,
                code,
                outcome.message,
                data={"httpStatus": 402, "challenges": [replacement]},
                challenge=replacement,
            )
        if outcome.status != "ok":
            return JSONResponse(
                status_code=503,
                content=_jsonrpc_error(req_id, -32603, "Paid MCP execution is temporarily unavailable."),
            )

        from billing import BillingResult

        request.state.x402_billing = BillingResult(
            verified=True,
            settled=True,  # chain-confirmed during verification
            billing_method="mpp",
            payer=outcome.source,
            amount_usdc=tool_cost,
            tx_hash=outcome.tx_hash,
        )
        request.state.mcp_mpp_challenge_id = outcome.challenge_id
        request.state.mcp_org_id = None
        request.state.mcp_auth_method = "mpp"
        return None

    @staticmethod
    async def _resolve_tool_cost(tool_name: str) -> int:
        from billing import get_current_pricing, get_tool_pricing_overrides, resolve_tool_cost

        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0
        return await resolve_tool_cost(tool_name, overrides, default_cost, get_settings().marketplace_enabled)

    @classmethod
    async def _x402_tool_requirements(cls, data: dict) -> list | None:
        """Exact requirements priced at the called tool's cost, or the flat exact default."""
        from billing import build_exact_payment_requirements, get_payment_requirements

        tool_name = _tool_call_name(data)
        tool_cost = await cls._resolve_tool_cost(tool_name) if tool_name is not None else 0
        if tool_cost > 0:
            return build_exact_payment_requirements(tool_cost) or None
        # Never offer upto on MCP: it needs a one-time Permit2 approval most MCP payers lack.
        return [req for req in get_payment_requirements() if getattr(req, "scheme", "exact") == "exact"] or None

    @classmethod
    async def _payment_probe_challenge(cls, request: Request) -> Response:
        """HTTP 402 for an anonymous non-JSON-RPC POST, priced and described as the declared Bazaar example call."""
        try:
            requirements = await cls._x402_tool_requirements(_MCP_BAZAAR_INPUT_EXAMPLE)
        except Exception:
            logger.warning("x402 MCP probe pricing unavailable", exc_info=True)
            return JSONResponse(
                status_code=503,
                content=_jsonrpc_error(None, -32603, "Paid MCP pricing is temporarily unavailable."),
            )
        response_kwargs: dict = {"resource": _mcp_402_resource(request), "extensions": _mcp_402_extensions()}
        if requirements is not None:
            response_kwargs["requirements"] = requirements
        return cls._x402_challenge(None, False, response_kwargs)

    @staticmethod
    def _x402_settlement_failed(request: Request, req_id: int | str | None) -> Response:
        challenge = getattr(request.state, "mcp_x402_challenge", None)
        mcp_signal, response_kwargs = challenge if isinstance(challenge, tuple) else (True, {})
        return MCPGatewayMiddleware._x402_challenge(
            req_id,
            mcp_signal,
            {**response_kwargs, "error": "Payment settlement failed; the tool result was withheld."},
        )

    @staticmethod
    def _x402_challenge(
        req_id: int | str | None, mcp_signal: bool, response_kwargs: dict, mpp_challenge: dict | None = None
    ) -> Response:
        from billing import build_402_headers, build_402_response_body

        body = build_402_response_body(**response_kwargs)
        if mcp_signal:
            return _mcp_payment_required(req_id, body, mpp_challenge)
        headers = dict(build_402_headers(**response_kwargs))
        if mpp_challenge is not None:
            from billing.mpp import mpp_www_authenticate

            headers["WWW-Authenticate"] = mpp_www_authenticate(mpp_challenge)
        return JSONResponse(status_code=402, content=body, headers=headers)

    async def _billing_gate(self, request: Request) -> tuple | Response | None:
        """Pre-request billing gate for ``tools/call`` requests.

        Resolves the per-call tool cost for both rails, then routes by auth
        method:

        * x402 callers — return the pending tuple so the post-response hook can
          settle on-chain via ``settle_payment``. No credit verification
          applies (access is payment-gated, not org-gated).
        * credit callers — check community tool existence/ownership and the
          caller's optional ``teardrop/max_cost_usdc`` bound, then verify the
          org's credit balance before allowing execution.

        Returns:
            None — billing disabled or not a billable ``tools/call`` request.
            tuple(org_id, tool_cost, tool_name, req_id) — ready for post-settle.
                ``org_id`` is None for anonymous x402 callers.
            Response — billing rejected (wrong endpoint or insufficient credits).
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

        from billing import is_promotional_credit, verify_credit

        tool_cost = await self._resolve_tool_cost(tool_name)

        # x402 callers are billed via on-chain settlement after execution.
        # Credit verification is a credit-rail concept and does not apply to
        # anonymous per-call x402 payments.
        if is_x402:
            from billing import reserve_payer_spend

            x402_billing = request.state.x402_billing
            if getattr(x402_billing, "billing_method", "x402") == "mpp":
                # The transfer is final before execution: a cap or failure-budget refusal here would
                # keep the payer's money without service. Those x402 guards bound unsettled exposure.
                request.state.mcp_call_event_id = str(uuid.uuid4())
                return (org_id, int(x402_billing.amount_usdc), tool_name, req_id)
            authorized = int(getattr(getattr(x402_billing, "payment_requirements", None), "amount", 0) or 0)
            if getattr(x402_billing, "scheme", "exact") == "exact" and authorized > 0:
                # Exact settles the full authorized amount, so account for that, not the catalog price.
                tool_cost = authorized
            payer = getattr(x402_billing, "payer", "")
            payment_header = self._payment_header(request)
            if not isinstance(payer, str) or not payer.strip() or not payment_header:
                await self._release_payment_claim(request)
                return JSONResponse(
                    status_code=402,
                    content=_jsonrpc_error(req_id, -32000, "Verified payer identity is required."),
                )
            payer_budget = await _payer_failure_budget(payer, req_id)
            if payer_budget is not None:
                await self._release_payment_claim(request)
                return payer_budget
            reserved = await reserve_payer_spend(
                payment_header,
                payer,
                tool_cost,
                settings.x402_payer_daily_spend_limit_usdc,
            )
            if not reserved:
                await self._release_payment_claim(request)
                return JSONResponse(
                    status_code=429,
                    content=_jsonrpc_error(req_id, -32029, "Anonymous x402 payer daily spend limit reached."),
                    headers={"X-RateLimit-Scope": "x402-payer"},
                )
            request.state.mcp_x402_reserved = True
            request.state.mcp_call_event_id = str(uuid.uuid4())
            return (org_id, tool_cost, tool_name, req_id)

        # Verified-email promotional credit must not create author earnings
        # through a direct marketplace MCP call. Platform tools are not
        # author-owned and remain available on this rail.
        if settings.onboarding_credit_enabled and _is_community_tool(tool_name) and await is_promotional_credit(org_id):
            logger.info("mcp promotional credit blocked marketplace tool org_id=%s tool=%s", org_id, tool_name)
            return JSONResponse(
                status_code=403,
                content=_jsonrpc_error(
                    req_id,
                    -32003,
                    "Marketplace author tools require a funded credit balance.",
                ),
            )

        if _is_community_tool(tool_name):
            from marketplace import get_marketplace_tool_by_name

            org_slug, bare_name = tool_name.split("/", 1)
            tool_row = await get_marketplace_tool_by_name(bare_name, org_slug)
            if tool_row is None:
                return JSONResponse(
                    status_code=404,
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            if tool_row.get("org_id") == org_id:
                # Self-calls would debit the author with zero earnings; testing has its own unbilled route.
                return JSONResponse(
                    status_code=403,
                    content=_jsonrpc_error(
                        req_id,
                        -32005,
                        "Authors cannot call their own marketplace tool here; use POST /tools/test-webhook.",
                    ),
                )

        call_meta = params.get("_meta")
        max_cost = call_meta.get(MCP_MAX_COST_META_KEY) if isinstance(call_meta, dict) else None
        if max_cost is not None:
            if type(max_cost) is not int or max_cost < 0:
                return JSONResponse(
                    status_code=400,
                    content=_jsonrpc_error(
                        req_id,
                        -32602,
                        f"_meta['{MCP_MAX_COST_META_KEY}'] must be a non-negative integer (atomic USDC).",
                    ),
                )
            if tool_cost > max_cost:
                return JSONResponse(
                    status_code=402,
                    content=_jsonrpc_error(
                        req_id,
                        -32004,
                        f"Tool price {tool_cost} atomic USDC exceeds max_cost_usdc {max_cost}.",
                    ),
                )

        billing = await verify_credit(
            org_id,
            tool_cost,
            principal_id=getattr(request.state, "mcp_principal_id", "") or None,
        )
        if not billing.verified:
            from teardrop.funnel_counters import SURFACE_MCP_402_CHALLENGE, record_discovery_hit

            record_discovery_hit(SURFACE_MCP_402_CHALLENGE)
            return JSONResponse(
                status_code=402,
                content=_jsonrpc_error(req_id, -32000, billing.error),
            )

        request.state.mcp_call_event_id = str(uuid.uuid4())
        return (org_id, tool_cost, tool_name, req_id)

    @staticmethod
    def _call_event_id(request: Request) -> str:
        event_id = getattr(request.state, "mcp_call_event_id", None)
        if not isinstance(event_id, str) or not event_id:
            event_id = str(uuid.uuid4())
            request.state.mcp_call_event_id = event_id
        return event_id

    @staticmethod
    async def _record_mcp_outcome(
        request: Request,
        org_id: str | None,
        tool_name: str,
        tool_cost: int,
        billing_method: str,
        settlement_status: str,
        settlement_tx: str = "",
    ) -> str:
        from billing.charges import record_charge
        from teardrop.usage import record_mcp_call_event

        event_id = MCPGatewayMiddleware._call_event_id(request)
        billing = getattr(request.state, "x402_billing", None)
        # On-chain rails (x402 and mpp) both carry a verified payer identity;
        # the credit rail never does.
        payer = getattr(billing, "payer", "") if billing_method in ("x402", "mpp") else ""
        if not isinstance(payer, str):
            payer = ""
        principal_id = getattr(request.state, "mcp_principal_id", "")
        asyncio.create_task(
            record_mcp_call_event(
                event_id,
                org_id or "",
                payer,
                tool_name,
                billing_method,
                tool_cost,
                settlement_status,
                settlement_tx,
            )
        )
        return await record_charge(
            source="mcp",
            invocation_id=event_id,
            org_id=org_id or "",
            principal_id=principal_id if isinstance(principal_id, str) else "",
            payer_address=payer,
            capability=tool_name,
            billing_method=billing_method,
            amount_usdc=tool_cost,
            status=settlement_status,
            settled_amount_usdc=tool_cost if settlement_status == "settled" else 0,
            settlement_tx=settlement_tx,
        )

    @staticmethod
    async def _enqueue_mcp_recovery(
        request: Request,
        org_id,
        tool_cost: int,
        billing_method: str,
        billing,
        principal_id: str | None = None,
        charge_id: str = "",
    ) -> None:
        """Enqueue a failed MCP settlement for asynchronous retry.

        The MCP gateway has no usage_event row, so the call event id anchors
        usage_event_id and run_id (``pending_settlements`` has no FK).
        """
        from billing.settlement import enqueue_failed_settlement

        payment_payload = None
        if billing is not None and getattr(billing, "payment_payload", None):
            payment_payload = str(billing.payment_payload)
        try:
            call_id = MCPGatewayMiddleware._call_event_id(request)
            await enqueue_failed_settlement(
                call_id,
                org_id or "",
                call_id,
                billing_method,
                tool_cost,
                payment_payload=payment_payload,
                principal_id=principal_id,
                charge_id=charge_id,
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
        org_id, tool_cost, tool_name, req_id = pending

        is_x402 = getattr(request.state, "x402_billing", None) is not None
        is_mpp = is_x402 and getattr(request.state.x402_billing, "billing_method", "x402") == "mpp"

        if is_mpp:
            # The transfer was final before execution, so the ledger records it as settled even when
            # the tool failed (there is nothing to withhold); the receipt goes out either way.
            mpp_billing = request.state.x402_billing
            await self._record_mcp_outcome(request, org_id, tool_name, tool_cost, "mpp", "settled", mpp_billing.tx_hash)
            response = await self._attach_mpp_receipt(response, request)
            if execution_failed:
                logger.info("mcp mpp execution failed after payment org=%s tool=%s", org_id, tool_name)
                return response
        elif execution_failed:
            logger.info("mcp settle skipped (execution failed) org=%s tool=%s", org_id, tool_name)
            await self._release_x402_reservation(request)
            await _record_unbilled_failure(request)
            return response
        elif is_x402:
            # Phase 3: on-chain settlement.
            from billing import settle_payment

            billing = request.state.x402_billing
            try:
                settled = await settle_payment(billing, actual_cost_usdc=tool_cost)
            except Exception:
                logger.warning("x402 MCP settlement failed", exc_info=True)
                settled = None

            if settled is None or not settled.settled:
                if settled is not None:
                    logger.warning("x402 MCP settlement rejected org=%s tool=%s error=%s", org_id, tool_name, settled.error)
                await self._record_mcp_outcome(request, org_id, tool_name, tool_cost, "x402", "failed")
                # Facilitator /verify does not prove funds, so an unsettled payment must not release the result.
                # The tool already ran unbilled (typical cause: unfunded wallet) — count it against the budget.
                await _record_unbilled_failure(request)
                return self._x402_settlement_failed(request, req_id)

            await self._record_mcp_outcome(
                request,
                org_id,
                tool_name,
                tool_cost,
                "x402",
                "settled",
                settled.tx_hash,
            )
            if settled.tx_hash:
                logger.info("x402 MCP settlement succeeded org=%s tool=%s tx_hash=%s", org_id, tool_name, settled.tx_hash)
            if self._meta_payment(request) is not None:
                response = await self._attach_payment_receipt(response, settled)
            from billing import build_payment_response_headers

            response.headers.update(
                build_payment_response_headers(
                    tx_hash=settled.tx_hash,
                    network=getattr(settled.payment_requirements, "network", "") or get_settings().x402_network,
                    payer=settled.payer,
                    amount_usdc=settled.amount_usdc,
                )
            )
        else:
            # Phase 2: credit debit.
            from billing import debit_credit

            debited, _ = await debit_credit(
                org_id,
                tool_cost,
                reason=f"mcp:{tool_name}",
                principal_id=getattr(request.state, "mcp_principal_id", "") or None,
            )
            if not debited:
                logger.warning("MCP debit failed org=%s tool=%s", org_id, tool_name)
                charge_id = await self._record_mcp_outcome(request, org_id, tool_name, tool_cost, "credit", "failed")
                await self._enqueue_mcp_recovery(
                    request,
                    org_id,
                    tool_cost,
                    "credit",
                    None,
                    principal_id=getattr(request.state, "mcp_principal_id", "") or None,
                    charge_id=charge_id,
                )
                return response
            await self._record_mcp_outcome(request, org_id, tool_name, tool_cost, "credit", "settled")

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
