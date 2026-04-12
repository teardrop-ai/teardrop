# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 [YOUR NAME OR ENTITY]. All rights reserved.
"""Teardrop FastAPI application.

Endpoints
---------
GET  /                       – redirect to /docs
GET  /health                 – health check
POST /token                  – tri-mode auth (client-creds, email+secret, SIWE)
GET  /auth/me                – return the authenticated user's identity
GET  /auth/siwe/nonce        – generate a single-use SIWE nonce
POST /agent/run              – AG-UI streaming endpoint (SSE)
GET  /.well-known/agent-card.json – A2A agent card for discoverability
POST /admin/orgs             – create organisation (admin)
POST /admin/users            – create user (admin)
POST /wallets/link           – link an additional wallet via SIWE
GET  /wallets/me             – list wallets for authenticated user
DELETE /wallets/{wallet_id}  – unlink a wallet
GET  /usage/me               – authenticated user's usage summary
GET  /admin/usage/{user_id}  – admin: usage for a specific user
GET  /admin/usage/org/{org_id} – admin: usage for an org
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import hmac
import json
import logging
import secrets
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent.graph import close_checkpointer, get_graph, init_checkpointer
from agent.state import AgentState
from auth import create_access_token, require_auth
from billing import (
    BillingResult,
    admin_topup_credit,
    build_402_headers,
    build_402_response_body,
    build_usdc_topup_requirements,
    calculate_run_cost_usdc,
    close_billing,
    create_stripe_embedded_session,
    credit_usdc_topup,
    debit_credit,
    delete_tool_pricing_override,
    get_billing_history,
    get_credit_balance,
    get_credit_history,
    get_current_pricing,
    get_invoice_by_run,
    get_invoices,
    get_revenue_summary,
    get_stripe_session_status,
    get_tool_pricing_overrides,
    handle_stripe_webhook,
    init_billing,
    record_settlement,
    settle_payment,
    upsert_tool_pricing_override,
    verify_and_settle_usdc_topup,
    verify_credit,
    verify_payment,
)
from cache import close_redis, get_redis, init_redis
from config import Settings, get_settings
from memory import (
    close_memory_db,
    count_memories,
    delete_all_org_memories,
    delete_memory,
    extract_and_store_memories,
    init_memory_db,
    list_memories,
    recall_memories,
    store_memory,
)
from mcp_client import (
    OrgMcpServer,
    build_mcp_langchain_tools,
    close_mcp_client_db,
    create_org_mcp_server,
    delete_org_mcp_server,
    discover_mcp_tools,
    get_org_mcp_server,
    init_mcp_client_db,
    invalidate_mcp_cache,
    list_org_mcp_servers,
    update_org_mcp_server,
)
from org_tools import (
    OrgTool,
    build_org_langchain_tools,
    close_org_tools_db,
    create_org_tool,
    delete_org_tool,
    get_org_tool,
    init_org_tools_db,
    invalidate_org_tools_cache,
    list_org_tools,
    update_org_tool,
)
from scripts.generate_keys import generate_keypair
from tools import registry
from usage import (
    UsageEvent,
    close_usage_db,
    get_usage_by_org,
    get_usage_by_user,
    init_usage_db,
    record_usage_event,
)
from users import (
    close_user_db,
    create_client_credential,
    create_org,
    create_user,
    get_client_credential_by_id,
    get_org_by_name,
    get_user_by_email,
    get_user_by_org_id,
    init_user_db,
    verify_secret,
)
from wallets import (
    close_wallets_db,
    consume_nonce,
    create_nonce,
    create_wallet,
    delete_wallet,
    get_wallet_by_address,
    get_wallets_by_user,
    init_wallets_db,
)

# ─── Logging ─────────────────────────────────────────────────────────────────

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.app_log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ─── FastAPI app ──────────────────────────────────────────────────────────────


def _validate_production_config(s: "Settings") -> None:
    """Warn on insecure defaults; fail-fast on critical misconfigurations in production."""
    is_prod = s.app_env == "production"
    prefix = "config"

    # jwt_client_secret — Render auto-generates this; other deployments must set it.
    if not s.jwt_client_secret:
        if is_prod:
            raise RuntimeError(
                "JWT_CLIENT_SECRET is not set. "
                "Generate a strong random secret and set it as an environment variable."
            )
        logger.warning(
            "%s ⚠  JWT_CLIENT_SECRET is empty — client-credentials auth is disabled", prefix
        )

    # CORS — open wildcard is acceptable for bearer-token APIs, but flag it loudly.
    if s.cors_origins in ("", "*"):
        if is_prod:
            logger.warning(
                "%s ⚠  CORS_ORIGINS is open (*) — recommended: restrict to your frontend origin",
                prefix,
            )
        else:
            logger.info("%s ·  CORS_ORIGINS open (*) — OK for local development", prefix)

    # SIWE domain — defaults to app_host (0.0.0.0) which will fail SIWE validation.
    if not s.siwe_domain and is_prod:
        logger.warning(
            "%s ⚠  SIWE_DOMAIN is not set — SIWE wallet auth will fail domain validation",
            prefix,
        )

    # Log a concise summary so operators can see the active config at a glance.
    logger.info(
        "%s   env=%s billing=%s cors=%s siwe_domain=%s",
        prefix,
        s.app_env,
        s.billing_enabled,
        s.cors_origins or "*(open)",
        s.siwe_domain or "(app_host fallback)",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for DB connections."""
    from migrations.runner import apply_pending

    # Ensure RSA keypair exists before config tries to read the key files.
    generate_keypair(Path(__file__).resolve().parent / "keys")

    # Warn on insecure defaults; raise on critical misconfigurations in production.
    _validate_production_config(settings)

    pool = await asyncpg.create_pool(settings.pg_dsn)
    app.state.pool = pool
    await apply_pending(pool)
    await init_redis(settings.redis_url)
    await init_checkpointer()
    await init_user_db(pool)
    await init_usage_db(pool)
    await init_wallets_db(pool)
    await init_billing(pool)
    await init_org_tools_db(pool)
    await init_mcp_client_db(pool)
    await init_memory_db(pool)
    yield
    await close_memory_db()
    await close_mcp_client_db()
    await close_org_tools_db()
    await close_billing()
    await close_wallets_db()
    await close_usage_db()
    await close_user_db()
    await close_checkpointer()
    await close_redis()
    await pool.close()
    app.state.pool = None


app = FastAPI(
    title="Teardrop",
    description=(
        "Intelligence beyond the browser. "
        "AG-UI streaming agent backed by LangGraph + Anthropic Claude."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ─── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Payment-Signature", "X-Payment"],
)

# ─── Rate limiting (sliding-window, Redis-first with in-process fallback) ─────

_rate_counters: dict[str, list[float]] = defaultdict(list)
_RATE_COUNTER_MAX_KEYS = 10_000

# Named tuple for rate limit check results.
RateLimitResult = tuple[bool, int, int]  # (allowed, remaining, reset_epoch)


async def _check_rate_limit(key: str, limit: int) -> RateLimitResult:
    """Check sliding-window rate limit for *key*.

    Returns ``(allowed, remaining, reset_epoch)``:
    - *allowed*: ``True`` when within limit.
    - *remaining*: requests left in the current window.
    - *reset_epoch*: Unix timestamp when the window resets.

    Uses Redis sorted sets when available; falls back to in-process dict.
    """
    now = time.time()
    window = 60.0
    reset_epoch = int(now + window)

    # ── Redis path (multi-container) ──────────────────────────────────────
    if (redis := get_redis()) is not None:
        redis_key = f"teardrop:rl:{key}"
        try:
            pipe = redis.pipeline()
            pipe.zremrangebyscore(redis_key, "-inf", f"({now - window}")
            pipe.zcard(redis_key)
            pipe.zadd(redis_key, {f"{now}_{secrets.token_hex(3)}": now})
            pipe.expire(redis_key, 61)
            _, count, _, _ = await pipe.execute()
            remaining = max(0, limit - count - 1)
            return count < limit, remaining, reset_epoch
        except Exception as exc:
            logger.warning("Redis rate limit check failed; falling back to in-process: %s", exc)

    # ── In-process fallback (single-container) ───────────────────────────
    history = _rate_counters[key]
    _rate_counters[key] = [t for t in history if now - t < window]
    if len(_rate_counters[key]) >= limit:
        return False, 0, reset_epoch
    _rate_counters[key].append(now)
    remaining = max(0, limit - len(_rate_counters[key]))
    if len(_rate_counters) > _RATE_COUNTER_MAX_KEYS:
        oldest_key = next(iter(_rate_counters))
        del _rate_counters[oldest_key]
    return True, remaining, reset_epoch


# ─── AG-UI event helpers ──────────────────────────────────────────────────────


def _sse_event(event_type: str, data: dict[str, Any]) -> dict[str, str]:
    """Format a Server-Sent Event dict for sse_starlette."""
    return {"event": event_type, "data": json.dumps(data)}


# AG-UI event type constants (aligned with ag-ui-protocol spec)
_EV_RUN_STARTED = "RUN_STARTED"
_EV_RUN_FINISHED = "RUN_FINISHED"
_EV_TEXT_MSG_START = "TEXT_MESSAGE_START"
_EV_TEXT_MSG_CONTENT = "TEXT_MESSAGE_CONTENT"
_EV_TEXT_MSG_END = "TEXT_MESSAGE_END"
_EV_TOOL_CALL_START = "TOOL_CALL_START"
_EV_TOOL_CALL_END = "TOOL_CALL_END"
_EV_STATE_SNAPSHOT = "STATE_SNAPSHOT"
_EV_SURFACE_UPDATE = "SURFACE_UPDATE"
_EV_USAGE_SUMMARY = "USAGE_SUMMARY"
_EV_BILLING_SETTLEMENT = "BILLING_SETTLEMENT"
_EV_ERROR = "ERROR"
_EV_DONE = "DONE"


# ─── Request / response models ────────────────────────────────────────────────


class AgentRunRequest(BaseModel):
    message: str = Field(..., description="User message to send to the agent", max_length=4096)
    thread_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Conversation thread ID for multi-turn sessions",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional extra context passed to the agent state metadata",
    )


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["System"])
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
                    "version": app.version,
                    "environment": settings.app_env,
                    "postgres": "error",
                },
            )
    else:
        postgres = "starting"
    return JSONResponse(
        content={
            "status": "ok",
            "service": "teardrop",
            "version": app.version,
            "environment": settings.app_env,
            "postgres": postgres,
        }
    )


@app.get("/.well-known/agent-card.json", tags=["A2A"])
async def agent_card() -> JSONResponse:
    """A2A agent card for discoverability and inter-agent communication."""
    return JSONResponse(
        content={
            "schema_version": "1.0",
            "name": "Teardrop",
            "description": (
                "Intelligence beyond the browser. A task-manager agent with LangGraph, AG-UI "
                "streaming, and A2UI rendering."
            ),
            "version": app.version,
            "url": f"http://{settings.app_host}:{settings.app_port}",
            "capabilities": {
                "streaming": True,
                "a2ui": True,
                "mcp_tools": True,
                "multi_turn": True,
                "human_in_the_loop": True,
                "billing": {
                    "enabled": settings.billing_enabled,
                    "scheme": settings.x402_scheme,
                    "network": settings.x402_network,
                    "payment_endpoint": "/agent/run",
                    "pricing_endpoint": "/billing/pricing",
                },
            },
            "protocols": ["ag-ui", "a2a", "mcp"],
            "endpoints": {
                "agent_run": "/agent/run",
                "health": "/health",
                "docs": "/docs",
            },
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


class TokenRequest(BaseModel):
    # Client-credentials flow (machine-to-machine) — backward compatible
    client_id: str | None = None
    client_secret: str | None = None
    # User-credentials flow (human users)
    email: str | None = None
    secret: str | None = None
    # SIWE flow (wallet users)
    siwe_message: str | None = None
    siwe_signature: str | None = None


@app.post("/token", tags=["Auth"])
async def token(body: TokenRequest, request: Request) -> JSONResponse:
    """Tri-mode token endpoint.

    Accepts one of:
      1. email+secret (user credentials)
      2. client_id+client_secret (machine-to-machine)
      3. siwe_message+siwe_signature (Sign-In with Ethereum)
    Returns a signed RS256 JWT.
    """
    client_ip = request.client.host if request.client else "unknown"
    allowed, remaining, reset_at = await _check_rate_limit(
        f"auth:{client_ip}", settings.rate_limit_auth_rpm
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
            headers={
                "X-RateLimit-Limit": str(settings.rate_limit_auth_rpm),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": "60",
            },
        )

    # ── User-credentials flow ──────────────────────────────────────────────
    if body.email and body.secret:
        user = await get_user_by_email(body.email)
        if user is None or not verify_secret(body.secret, user.hashed_secret, user.salt):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user credentials",
            )
        access_token = create_access_token(
            subject=user.id,
            extra_claims={
                "org_id": user.org_id,
                "email": user.email,
                "role": user.role,
                "auth_method": "email",
            },
        )
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.jwt_access_token_expire_minutes * 60,
            }
        )

    # ── Client-credentials flow ────────────────────────────────────────────────
    if body.client_id and body.client_secret:
        # Try DB-backed org credential first (org-scoped M2M)
        db_cred = await get_client_credential_by_id(body.client_id)
        if db_cred is not None:
            if not verify_secret(body.client_secret, db_cred.hashed_secret, db_cred.salt):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid client credentials",
                )
            org_id = db_cred.org_id
        else:
            # Fall back to config-based credential (backward compat — org_id is empty)
            if not settings.jwt_client_secret:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid client credentials",
                )
            if body.client_id != settings.jwt_client_id or not hmac.compare_digest(
                body.client_secret, settings.jwt_client_secret
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid client credentials",
                )
            org_id = ""
        access_token = create_access_token(
            subject=body.client_id,
            extra_claims={"auth_method": "client_credentials", "org_id": org_id},
        )
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.jwt_access_token_expire_minutes * 60,
            }
        )

    # ── SIWE flow (Sign-In with Ethereum) ──────────────────────────────────
    if body.siwe_message and body.siwe_signature:
        return await _handle_siwe_login(body.siwe_message, body.siwe_signature)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Provide email+secret, client_id+client_secret, or siwe_message+siwe_signature.",
    )


async def _handle_siwe_login(siwe_message: str, siwe_signature: str) -> JSONResponse:
    """Verify a SIWE message, auto-register if needed, and return a JWT."""
    import siwe as siwe_errors
    from siwe import SiweMessage

    try:
        msg = SiweMessage.from_message(siwe_message)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed SIWE message")

    # Validate domain
    expected_domain = settings.effective_siwe_domain
    if msg.domain != expected_domain:
        raise HTTPException(status_code=400, detail=f"Domain mismatch: expected {expected_domain}")

    # Consume nonce (single-use + TTL)
    if not await consume_nonce(msg.nonce, settings.siwe_nonce_ttl_seconds):
        raise HTTPException(status_code=401, detail="Invalid or expired nonce")

    # Verify EIP-191 signature
    try:
        msg.verify(signature=siwe_signature)
    except (
        siwe_errors.ExpiredMessage,
        siwe_errors.InvalidSignature,
        siwe_errors.DomainMismatch,
        siwe_errors.NonceMismatch,
        siwe_errors.MalformedSession,
    ):
        raise HTTPException(status_code=401, detail="SIWE signature verification failed")
    except Exception:
        raise HTTPException(status_code=401, detail="SIWE verification error")

    # Checksummed address from the verified message
    from web3 import Web3

    address = Web3.to_checksum_address(msg.address)
    chain_id = int(msg.chain_id) if msg.chain_id else 1

    # Look up existing wallet
    wallet = await get_wallet_by_address(address, chain_id)

    if wallet is None:
        # Org may already exist from a previous partial registration
        org_name = f"wallet-{address[:10].lower()}"
        existing_org = await get_org_by_name(org_name)
        if existing_org:
            org = existing_org
            existing_user = await get_user_by_org_id(org.id)
            user = existing_user or await create_user(
                email=f"{address.lower()}@wallet",
                secret=secrets.token_urlsafe(32),
                org_id=org.id,
                role="user",
            )
        else:
            org = await create_org(org_name)
            user = await create_user(
                email=f"{address.lower()}@wallet",
                secret=secrets.token_urlsafe(32),
                org_id=org.id,
                role="user",
            )
        wallet = await create_wallet(
            address=address,
            chain_id=chain_id,
            user_id=user.id,
            org_id=org.id,
            is_primary=True,
        )
        logger.info("SIWE auto-registered user=%s address=%s", user.id, address)

    access_token = create_access_token(
        subject=wallet.user_id,
        extra_claims={
            "org_id": wallet.org_id,
            "address": address,
            "chain_id": chain_id,
            "auth_method": "siwe",
            "role": "user",
            "email": f"{address.lower()}@wallet",
        },
    )
    return JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.jwt_access_token_expire_minutes * 60,
        }
    )


@app.get("/auth/me", tags=["Auth"])
async def auth_me(payload: dict = Depends(require_auth)) -> JSONResponse:
    """Return identity claims for the currently authenticated user.

    Decodes the Bearer JWT and echoes back the stable claims so the
    frontend can identify the user without an extra database round-trip.
    """
    body: dict = {
        "user_id": payload["sub"],
        "org_id": payload.get("org_id", ""),
        "role": payload.get("role", "user"),
        "auth_method": payload.get("auth_method", ""),
        "email": payload.get("email", ""),
    }
    # Include wallet-specific fields only for SIWE sessions.
    if payload.get("auth_method") == "siwe":
        body["address"] = payload.get("address", "")
        body["chain_id"] = payload.get("chain_id", 1)
    return JSONResponse(content=body)


@app.get("/auth/siwe/nonce", tags=["Auth"])
async def siwe_nonce(request: Request) -> JSONResponse:
    """Generate a single-use nonce for SIWE authentication."""
    client_ip = request.client.host if request.client else "unknown"
    allowed, remaining, reset_at = await _check_rate_limit(
        f"auth:{client_ip}", settings.rate_limit_auth_rpm
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={
                "X-RateLimit-Limit": str(settings.rate_limit_auth_rpm),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": "60",
            },
        )
    nonce = await create_nonce()
    return JSONResponse(content={"nonce": nonce})


@app.post("/agent/run", tags=["Agent"])
async def agent_run(
    body: AgentRunRequest,
    request: Request,
    payload: dict = Depends(require_auth),
) -> EventSourceResponse:
    """AG-UI streaming endpoint.

    Accepts a user message and streams AG-UI-compatible Server-Sent Events
    until the agent completes or errors.  Supports multi-turn via thread_id.
    Thread state is scoped to the authenticated user.
    """
    client_ip = request.client.host if request.client else "unknown"
    user_id: str = payload["sub"]
    allowed, remaining, reset_at = await _check_rate_limit(
        f"run:{user_id}", settings.rate_limit_agent_rpm
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
            headers={
                "X-RateLimit-Limit": str(settings.rate_limit_agent_rpm),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": "60",
            },
        )

    org_id: str = payload.get("org_id", "")
    run_id = str(uuid.uuid4())
    scoped_thread_id = f"{user_id}:{body.thread_id}"
    logger.info(
        "agent_run start run_id=%s thread_id=%s user=%s",
        run_id,
        scoped_thread_id,
        user_id,
    )

    # ── Billing gate ────────────────────────────────────────────────────────
    # Dispatches based on auth_method:
    #   siwe              → x402 on-chain payment header (verify_payment)
    #   client_credentials / email → org prepaid credit balance (verify_credit)
    billing = BillingResult()
    auth_method = payload.get("auth_method", "")
    if settings.billing_enabled and auth_method in settings.billable_auth_methods:
        if auth_method == "siwe":
            payment_header = request.headers.get("payment-signature") or request.headers.get(
                "x-payment"
            )
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
        else:
            # Credit-based billing: ensure org has enough balance to cover at
            # least one run at the current flat-rate floor.
            pricing = await get_current_pricing()
            min_required = pricing.run_price_usdc if pricing is not None else 0
            billing = await verify_credit(org_id, min_required)
            if not billing.verified:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=billing.error,
                )

    async def _stream() -> AsyncIterator[dict[str, str]]:
        start_time = time.monotonic()
        yield _sse_event(_EV_RUN_STARTED, {"run_id": run_id, "thread_id": body.thread_id})

        graph = await get_graph()
        org_lc_tools, org_tools_by_name = await build_org_langchain_tools(org_id)

        # ── Merge MCP server tools ────────────────────────────────────────
        try:
            mcp_tools, mcp_by_name = await build_mcp_langchain_tools(org_id)
            org_lc_tools = list(org_lc_tools) + mcp_tools
            org_tools_by_name = {**org_tools_by_name, **mcp_by_name}
        except Exception:
            logger.debug("MCP tool discovery failed for org_id=%s", org_id, exc_info=True)

        # ── Recall relevant memories for this org ─────────────────────────
        recalled: list[str] = []
        mem_settings = get_settings()
        if mem_settings.memory_enabled:
            try:
                entries = await recall_memories(org_id, body.message, mem_settings.memory_top_k)
                recalled = [e.content for e in entries]
            except Exception:
                logger.debug("Memory recall failed for org_id=%s", org_id, exc_info=True)

        initial_state = AgentState(
            messages=[HumanMessage(content=body.message)],
            metadata={
                **body.context,
                "thread_id": scoped_thread_id,
                "run_id": run_id,
                "user_id": user_id,
                "org_id": org_id,
                "_usage": {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0, "tool_names": []},
                "_org_tools": org_lc_tools,
                "_org_tools_by_name": org_tools_by_name,
                "_memories": recalled,
            },
        )
        config = {"configurable": {"thread_id": scoped_thread_id}}

        try:
            async for event in graph.astream_events(
                initial_state.model_dump(),
                config=config,
                version="v2",
            ):
                event_name: str = event.get("event", "")
                event_data: dict[str, Any] = event.get("data", {})
                node_name: str = event.get("name", "")

                # --- Text streaming from the planner (LLM tokens) ---
                if event_name == "on_chat_model_stream":
                    chunk = event_data.get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        msg_id = event.get("run_id", run_id)
                        yield _sse_event(
                            _EV_TEXT_MSG_CONTENT,
                            {"message_id": msg_id, "delta": chunk.content},
                        )

                # --- Tool call start ---
                elif event_name == "on_tool_start":
                    yield _sse_event(
                        _EV_TOOL_CALL_START,
                        {
                            "tool_call_id": event.get("run_id", ""),
                            "tool_name": node_name,
                            "args": event_data.get("input", {}),
                        },
                    )

                # --- Tool call end ---
                elif event_name == "on_tool_end":
                    yield _sse_event(
                        _EV_TOOL_CALL_END,
                        {
                            "tool_call_id": event.get("run_id", ""),
                            "tool_name": node_name,
                            "output": str(event_data.get("output", "")),
                        },
                    )

                # --- Node outputs (state snapshots) ---
                elif event_name == "on_chain_end" and node_name == "ui_generator":
                    output = event_data.get("output", {})
                    ui_components = output.get("ui_components", [])
                    if ui_components:
                        yield _sse_event(
                            _EV_SURFACE_UPDATE,
                            {
                                "surface_id": run_id,
                                "components": [
                                    c if isinstance(c, dict) else c.model_dump()
                                    for c in ui_components
                                ],
                            },
                        )

                # --- Yield control to allow concurrent requests ---
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            logger.info("agent_run cancelled run_id=%s", run_id)
            yield _sse_event(_EV_ERROR, {"run_id": run_id, "error": "Request cancelled."})
            return

        except Exception as exc:
            logger.error("agent_run error run_id=%s: %s", run_id, exc, exc_info=True)
            # Do not leak internal exception details to clients in production.
            error_msg = (
                f"Agent error: {exc}"
                if settings.app_env != "production"
                else "An internal error occurred. Check server logs for details."
            )
            yield _sse_event(
                _EV_ERROR,
                {"run_id": run_id, "error": error_msg},
            )
            return

        # ── Usage accounting (log-only, never blocks) ─────────────────────
        duration_ms = int((time.monotonic() - start_time) * 1000)
        usage_data: dict[str, Any] = {}
        try:
            state_snapshot = await graph.aget_state(config)
            usage_data = (state_snapshot.values or {}).get("metadata", {}).get("_usage", {})
        except Exception:
            logger.debug("Could not retrieve final state for usage", exc_info=True)

        # Calculate usage-based cost from live pricing rule (never blocks the stream).
        cost_usdc = 0
        try:
            cost_usdc = await calculate_run_cost_usdc(usage_data)
        except Exception:
            logger.debug("Could not calculate run cost", exc_info=True)

        usage_event = UsageEvent(
            user_id=user_id,
            org_id=org_id,
            thread_id=scoped_thread_id,
            run_id=run_id,
            tokens_in=usage_data.get("tokens_in", 0),
            tokens_out=usage_data.get("tokens_out", 0),
            tool_calls=usage_data.get("tool_calls", 0),
            tool_names=usage_data.get("tool_names", []),
            duration_ms=duration_ms,
            cost_usdc=cost_usdc,
        )
        await record_usage_event(usage_event)

        # ── Extract and store memories (fire-and-forget) ─────────────────
        if mem_settings.memory_enabled:
            try:
                state_msgs = (state_snapshot.values or {}).get("messages", [])
                if state_msgs:
                    asyncio.create_task(
                        extract_and_store_memories(org_id, user_id, state_msgs, run_id)
                    )
            except Exception:
                logger.debug("Memory extraction kickoff failed", exc_info=True)

        # ── Settlement / credit debit (after usage recorded) ─────────────
        if billing.verified:
            if billing.billing_method == "credit":
                # Debit actual run cost from org's prepaid balance.
                success = await debit_credit(org_id, cost_usdc, reason=f"run:{run_id}")
                if success:
                    await record_settlement(usage_event.id, cost_usdc, "", "settled")
                    yield _sse_event(
                        _EV_BILLING_SETTLEMENT,
                        {
                            "run_id": run_id,
                            "amount_usdc": cost_usdc,
                            "tx_hash": "",
                            "network": "credit",
                        },
                    )
                else:
                    await record_settlement(usage_event.id, cost_usdc, "", "failed")
                    logger.warning("Credit debit failed run_id=%s org_id=%s", run_id, org_id)
            else:
                # x402 on-chain settlement.
                billing_settled = await settle_payment(billing)
                if billing_settled.settled:
                    await record_settlement(
                        usage_event.id,
                        billing_settled.amount_usdc,
                        billing_settled.tx_hash,
                        "settled",
                    )
                    yield _sse_event(
                        _EV_BILLING_SETTLEMENT,
                        {
                            "run_id": run_id,
                            "amount_usdc": billing_settled.amount_usdc,
                            "tx_hash": billing_settled.tx_hash,
                            "network": settings.x402_network,
                        },
                    )
                else:
                    await record_settlement(usage_event.id, 0, "", "failed")
                    logger.warning(
                        "Settlement failed run_id=%s: %s",
                        run_id,
                        billing_settled.error,
                    )

        yield _sse_event(
            _EV_USAGE_SUMMARY,
            {
                "run_id": run_id,
                "tokens_in": usage_event.tokens_in,
                "tokens_out": usage_event.tokens_out,
                "tool_calls": usage_event.tool_calls,
                "duration_ms": usage_event.duration_ms,
                "cost_usdc": usage_event.cost_usdc,
            },
        )
        yield _sse_event(_EV_RUN_FINISHED, {"run_id": run_id})
        yield _sse_event(_EV_DONE, {"run_id": run_id})

    return EventSourceResponse(_stream())


# ─── Auth helpers ─────────────────────────────────────────────────────────────


async def require_admin(
    payload: dict = Depends(require_auth),
) -> dict:
    """FastAPI dependency — requires an authenticated user with role=admin."""
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return payload


# ─── Admin endpoints ─────────────────────────────────────────────────────────


class CreateOrgRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class CreateUserRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    secret: str = Field(..., min_length=8, max_length=128)
    org_id: str
    role: str = "user"


@app.post("/admin/orgs", tags=["Admin"])
async def admin_create_org(
    body: CreateOrgRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create a new organisation (admin only)."""
    org = await create_org(body.name)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": org.id, "name": org.name},
    )


@app.post("/admin/users", tags=["Admin"])
async def admin_create_user(
    body: CreateUserRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create a new user within an org (admin only)."""
    user = await create_user(
        email=body.email,
        secret=body.secret,
        org_id=body.org_id,
        role=body.role,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": user.id, "email": user.email, "org_id": user.org_id, "role": user.role},
    )


class CreateClientCredentialsRequest(BaseModel):
    org_id: str


@app.post("/admin/client-credentials", tags=["Admin"])
async def admin_create_client_credentials(
    body: CreateClientCredentialsRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Create org-scoped M2M client credentials (admin only).

    The client_secret is returned exactly once — store it immediately.
    """
    cred, plaintext_secret = await create_client_credential(body.org_id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "client_id": cred.client_id,
            "client_secret": plaintext_secret,
            "org_id": cred.org_id,
            "created_at": cred.created_at.isoformat(),
        },
    )


# ─── Wallet endpoints ─────────────────────────────────────────────────────────


class LinkWalletRequest(BaseModel):
    siwe_message: str
    siwe_signature: str


@app.post("/wallets/link", tags=["Wallets"])
async def link_wallet(
    body: LinkWalletRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Link an additional wallet to the authenticated user via SIWE."""
    import siwe as siwe_errors
    from siwe import SiweMessage

    try:
        msg = SiweMessage.from_message(body.siwe_message)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed SIWE message")

    expected_domain = settings.effective_siwe_domain
    if msg.domain != expected_domain:
        raise HTTPException(status_code=400, detail=f"Domain mismatch: expected {expected_domain}")

    if not await consume_nonce(msg.nonce, settings.siwe_nonce_ttl_seconds):
        raise HTTPException(status_code=401, detail="Invalid or expired nonce")

    try:
        msg.verify(signature=body.siwe_signature)
    except (
        siwe_errors.ExpiredMessage,
        siwe_errors.InvalidSignature,
        siwe_errors.DomainMismatch,
        siwe_errors.NonceMismatch,
        siwe_errors.MalformedSession,
    ):
        raise HTTPException(status_code=401, detail="SIWE signature verification failed")
    except Exception:
        raise HTTPException(status_code=401, detail="SIWE verification error")

    from web3 import Web3

    address = Web3.to_checksum_address(msg.address)
    chain_id = int(msg.chain_id) if msg.chain_id else 1

    existing = await get_wallet_by_address(address, chain_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Wallet already linked")

    wallet = await create_wallet(
        address=address,
        chain_id=chain_id,
        user_id=payload["sub"],
        org_id=payload.get("org_id", ""),
        is_primary=False,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"id": wallet.id, "address": wallet.address, "chain_id": wallet.chain_id},
    )


@app.get("/wallets/me", tags=["Wallets"])
async def list_wallets(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List all wallets linked to the authenticated user."""
    wallets = await get_wallets_by_user(payload["sub"])
    return JSONResponse(
        content=[
            {
                "id": w.id,
                "address": w.address,
                "chain_id": w.chain_id,
                "is_primary": w.is_primary,
                "created_at": w.created_at.isoformat(),
            }
            for w in wallets
        ]
    )


@app.delete("/wallets/{wallet_id}", tags=["Wallets"])
async def unlink_wallet(
    wallet_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Unlink a wallet from the authenticated user."""
    deleted = await delete_wallet(wallet_id, payload["sub"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Wallet not found or not owned by you")
    return JSONResponse(content={"status": "deleted"})


# ─── Usage endpoints ─────────────────────────────────────────────────────────


@app.get("/usage/me", tags=["Usage"])
async def usage_me(
    payload: dict = Depends(require_auth),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for the authenticated user."""
    from datetime import datetime

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_user(payload["sub"], start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())


@app.get("/admin/usage/{user_id}", tags=["Admin"])
async def admin_usage_user(
    user_id: str,
    _admin: dict = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for a specific user (admin only)."""
    from datetime import datetime

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_user(user_id, start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())


@app.get("/admin/usage/org/{org_id}", tags=["Admin"])
async def admin_usage_org(
    org_id: str,
    _admin: dict = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for an entire org (admin only)."""
    from datetime import datetime

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_org(org_id, start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())


# ─── Billing endpoints ───────────────────────────────────────────────────────


@app.get("/billing/pricing", tags=["Billing"])
async def billing_pricing() -> JSONResponse:
    """Return current pricing rules (public)."""
    if not settings.billing_enabled:
        return JSONResponse(content={"billing_enabled": False})
    pricing = await get_current_pricing()
    if pricing is None:
        return JSONResponse(content={"billing_enabled": True, "pricing": None})
    tool_overrides = await get_tool_pricing_overrides()
    pricing_data = pricing.model_dump(mode="json")
    pricing_data["tool_overrides"] = tool_overrides
    return JSONResponse(
        content={
            "billing_enabled": True,
            "pricing": pricing_data,
            "network": settings.x402_network,
        }
    )


@app.get("/billing/history", tags=["Billing"])
async def billing_history(
    payload: dict = Depends(require_auth),
    limit: int = 50,
) -> JSONResponse:
    """Return settlement history for the authenticated user."""
    history = await get_billing_history(payload["sub"], min(limit, 200))
    return JSONResponse(
        content=[{**row, "created_at": row["created_at"].isoformat()} for row in history]
    )


class ToolPricingOverrideRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=100)
    cost_usdc: int = Field(..., ge=0, le=100_000_000)
    description: str = Field("", max_length=500)


@app.post("/admin/pricing/tools", tags=["Admin"])
async def admin_upsert_tool_pricing(
    body: ToolPricingOverrideRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Set or update the per-call cost for a specific tool (admin only).

    Rejects tool names that are not registered in the tool registry.
    """
    known_names = {t.name for t in registry.list_latest(include_deprecated=True)}
    if body.tool_name not in known_names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown tool name: {body.tool_name!r}. Must be one of: {sorted(known_names)}",
        )
    await upsert_tool_pricing_override(body.tool_name, body.cost_usdc, body.description)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "tool_name": body.tool_name,
            "cost_usdc": body.cost_usdc,
            "description": body.description,
            "updated": True,
        },
    )


@app.delete("/admin/pricing/tools/{tool_name}", tags=["Admin"])
async def admin_delete_tool_pricing(
    tool_name: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Remove a per-tool pricing override, reverting to the global default (admin only)."""
    deleted = await delete_tool_pricing_override(tool_name)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pricing override found for tool: {tool_name!r}",
        )
    return JSONResponse(content={"deleted": True, "tool_name": tool_name})


@app.get("/admin/billing/revenue", tags=["Admin"])
async def admin_billing_revenue(
    _admin: dict = Depends(require_admin),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Aggregate settled revenue by period (admin only)."""
    from datetime import datetime

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_revenue_summary(start_dt, end_dt)
    return JSONResponse(content=summary)


@app.get("/billing/balance", tags=["Billing"])
async def billing_balance(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return the authenticated org's current credit balance."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — credit balance requires an org-scoped credential.",
        )
    balance = await get_credit_balance(org_id)
    return JSONResponse(content={"org_id": org_id, "balance_usdc": balance})


@app.get("/billing/invoices", tags=["Billing"])
async def billing_invoices(
    payload: dict = Depends(require_auth),
    limit: int = 50,
    cursor: str | None = None,
) -> JSONResponse:
    """Return per-run invoice records for the authenticated user (cursor paginated)."""
    from datetime import datetime

    cursor_dt = datetime.fromisoformat(cursor) if cursor else None
    invoices = await get_invoices(payload["sub"], min(limit, 200), cursor_dt)
    serialized = [{**row, "created_at": row["created_at"].isoformat()} for row in invoices]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


@app.get("/billing/invoice/{run_id}", tags=["Billing"])
async def billing_invoice_by_run(
    run_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return a single run receipt scoped to the authenticated user."""
    invoice = await get_invoice_by_run(run_id, payload["sub"])
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return JSONResponse(content={**invoice, "created_at": invoice["created_at"].isoformat()})


class TopupRequest(BaseModel):
    org_id: str
    amount_usdc: int = Field(..., gt=0)


@app.post("/admin/credits/topup", tags=["Admin"])
async def admin_credits_topup(
    body: TopupRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Top up an org's prepaid credit balance (admin only)."""
    new_balance = await admin_topup_credit(body.org_id, body.amount_usdc)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"org_id": body.org_id, "new_balance_usdc": new_balance},
    )


@app.get("/billing/credit-history", tags=["Billing"])
async def billing_credit_history(
    payload: dict = Depends(require_auth),
    operation: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> JSONResponse:
    """Return credit ledger entries for the authenticated org (cursor paginated)."""
    from datetime import datetime

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — credit history requires an org-scoped credential.",
        )
    if operation is not None and operation not in ("debit", "topup"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="operation must be 'debit' or 'topup'",
        )
    cursor_dt = datetime.fromisoformat(cursor) if cursor else None
    entries = await get_credit_history(org_id, operation, min(limit, 200), cursor_dt)
    serialized = [{**row, "created_at": row["created_at"].isoformat()} for row in entries]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


# ─── Stripe top-up endpoints ─────────────────────────────────────────────────


class StripeTopupRequest(BaseModel):
    amount_cents: int = Field(
        ..., ge=100, le=1_000_000, description="USD cents (100 = $1.00, max $10,000)"
    )
    return_url: str = Field(
        ...,
        min_length=20,
        max_length=500,
        description="HTTPS return URL with {CHECKOUT_SESSION_ID} template",
    )


@app.post("/billing/topup/stripe", tags=["Billing"])
async def billing_topup_stripe(
    body: StripeTopupRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Create a Stripe Checkout session for embedded checkout (prepaid credit top-up).

    Returns client_secret and session_id for embedding a Stripe form in the frontend.
    """
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — top-up requires an org-scoped credential.",
        )
    user_id: str = payload.get("sub", "")
    session_data = await create_stripe_embedded_session(
        org_id, user_id, body.amount_cents, body.return_url
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=session_data,
    )


@app.post("/billing/topup/webhook", include_in_schema=False)
async def billing_topup_webhook(request: Request) -> JSONResponse:
    """Stripe webhook receiver for checkout.session.completed events."""
    import stripe as _stripe  # noqa: PLC0415

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        await handle_stripe_webhook(payload, sig_header)
    except _stripe.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe signature"
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload"
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})


@app.get("/billing/topup/stripe/status", tags=["Billing"])
async def billing_topup_stripe_status(
    session_id: str = Query(..., description="Stripe Checkout session ID"),
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Retrieve the status of a Stripe Checkout session and credit balance upon completion.

    Returns { status: 'open' | 'complete' | 'expired', new_balance_fmt?: '$X.XX' }
    new_balance_fmt is included only when status is 'complete'.

    Returns HTTP 403 if the session does not belong to the authenticated org.
    """
    import stripe as _stripe  # noqa: PLC0415

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — status check requires an org-scoped credential.",
        )

    try:
        status_data = await get_stripe_session_status(session_id, org_id)
        return JSONResponse(status_code=status.HTTP_200_OK, content=status_data)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session does not belong to this org",
        )
    except _stripe.error.InvalidRequestError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe session not found",
        )
    except Exception as e:
        logger.exception("stripe status check failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to check Stripe session status",
        )


# ─── USDC on-chain top-up endpoints ──────────────────────────────────────────


@app.get("/billing/topup/usdc/requirements", tags=["Billing"])
async def billing_usdc_topup_requirements(
    amount_usdc: int = Query(
        ...,
        ge=1_000_000,
        le=10_000_000_000,
        description="Amount in atomic USDC (6 decimals). Min $1.00 = 1_000_000. Max $10,000 = 10_000_000_000.",
    ),
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return x402 PaymentRequirements to sign for a USDC on-chain top-up.

    The client should sign the returned requirements using EIP-3009
    (same flow as /agent/run X-PAYMENT), then POST the signed
    payment_header to /billing/topup/usdc.

    Returns 503 if BILLING_ENABLED is false.
    """
    try:
        reqs = build_usdc_topup_requirements(amount_usdc)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"USDC top-up unavailable: {exc}",
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "accepts": [r.model_dump() if hasattr(r, "model_dump") else r.__dict__ for r in reqs],
            "x402Version": 2,
        },
    )


class UsdcTopupRequest(BaseModel):
    amount_usdc: int = Field(
        ...,
        ge=1_000_000,
        le=10_000_000_000,
        description="Amount in atomic USDC (6 decimals). Min $1.00 = 1_000_000.",
    )
    payment_header: str = Field(
        ..., description="Base64-encoded signed EIP-3009 PaymentPayload (X-PAYMENT format)."
    )


@app.post("/billing/topup/usdc", tags=["Billing"])
async def billing_topup_usdc(
    body: UsdcTopupRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Top up org credit balance by submitting a signed USDC on-chain payment.

    The client obtains payment requirements from GET /billing/topup/usdc/requirements,
    signs them using EIP-3009 (MetaMask / wallet), and posts the base64-encoded
    payment_header here.

    The server verifies the signature, settles on-chain via the x402 facilitator,
    then credits the authenticated org's balance atomically.

    Returns 402 if signature verification fails, 409 if the tx_hash was already
    processed (duplicate submission), 503 if billing is disabled.
    """
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — top-up requires an org-scoped credential.",
        )

    try:
        result = await verify_and_settle_usdc_topup(body.payment_header, body.amount_usdc)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"USDC top-up unavailable: {exc}",
        )

    if not result.settled:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=result.error or "Payment verification or settlement failed.",
        )

    new_balance = await credit_usdc_topup(org_id, result.amount_usdc, result.tx_hash)
    if new_balance is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transaction {result.tx_hash} was already processed.",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "credited",
            "amount_usdc": result.amount_usdc,
            "balance_usdc": new_balance,
            "tx_hash": result.tx_hash,
        },
    )


# ─── Custom Tool CRUD endpoints ──────────────────────────────────────────────


class CreateOrgToolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(..., min_length=1, max_length=500)
    input_schema: dict = Field(..., description="JSON Schema for tool input parameters")
    webhook_url: str = Field(..., max_length=2048)
    webhook_method: str = Field(default="POST", pattern=r"^(GET|POST|PUT)$")
    auth_header_name: str | None = Field(default=None, max_length=64)
    auth_header_value: str | None = Field(default=None, max_length=4096)
    timeout_seconds: int = Field(default=10, ge=1, le=30)


class UpdateOrgToolRequest(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=2048)
    webhook_method: str | None = Field(default=None, pattern=r"^(GET|POST|PUT)$")
    auth_header_name: str | None = None
    auth_header_value: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)
    is_active: bool | None = None


class OrgToolResponse(BaseModel):
    id: str
    org_id: str
    name: str
    description: str
    input_schema: dict
    webhook_url: str
    webhook_method: str
    has_auth: bool
    timeout_seconds: int
    is_active: bool
    created_at: str
    updated_at: str


def _org_tool_to_response(tool: OrgTool) -> dict[str, Any]:
    """Convert an OrgTool model to a JSON-serialisable dict."""
    return {
        "id": tool.id,
        "org_id": tool.org_id,
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "webhook_url": tool.webhook_url,
        "webhook_method": tool.webhook_method,
        "has_auth": tool.has_auth,
        "timeout_seconds": tool.timeout_seconds,
        "is_active": tool.is_active,
        "created_at": tool.created_at.isoformat(),
        "updated_at": tool.updated_at.isoformat(),
    }


@app.post("/tools", tags=["Tools"])
async def create_tool(
    body: CreateOrgToolRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Register a custom webhook-backed tool for the authenticated org."""
    from jsonschema import Draft7Validator, SchemaError  # noqa: PLC0415

    from tools.definitions.http_fetch import validate_url  # noqa: PLC0415

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    user_id: str = payload.get("sub", "")

    # Validate JSON Schema
    try:
        Draft7Validator.check_schema(body.input_schema)
    except SchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid input_schema: {exc.message}",
        )

    # SSRF check
    ssrf_err = validate_url(body.webhook_url)
    if ssrf_err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsafe webhook URL: {ssrf_err}",
        )

    # HTTPS enforcement in production
    s = get_settings()
    if s.app_env == "production" and not body.webhook_url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Webhook URL must use HTTPS in production.",
        )

    # Global name collision check
    if registry.get(body.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tool name '{body.name}' conflicts with a built-in tool.",
        )

    # Auth header consistency
    if body.auth_header_value and not body.auth_header_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auth_header_name is required when auth_header_value is provided.",
        )

    try:
        tool = await create_org_tool(
            org_id=org_id,
            name=body.name,
            description=body.description,
            input_schema=body.input_schema,
            webhook_url=body.webhook_url,
            webhook_method=body.webhook_method,
            auth_header_name=body.auth_header_name,
            auth_header_value=body.auth_header_value,
            timeout_seconds=body.timeout_seconds,
            actor_id=user_id,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tool '{body.name}' already exists for this org.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    await invalidate_org_tools_cache(org_id)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_org_tool_to_response(tool))


@app.get("/tools", tags=["Tools"])
async def list_tools(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List custom tools for the authenticated org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    tools = await list_org_tools(org_id)
    return JSONResponse(content=[_org_tool_to_response(t) for t in tools])


@app.get("/tools/{tool_id}", tags=["Tools"])
async def get_tool(
    tool_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get a specific custom tool by ID."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    tool = await get_org_tool(tool_id, org_id)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found.")
    return JSONResponse(content=_org_tool_to_response(tool))


@app.patch("/tools/{tool_id}", tags=["Tools"])
async def patch_tool(
    tool_id: str,
    body: UpdateOrgToolRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Update a custom tool (partial update)."""
    from tools.definitions.http_fetch import validate_url  # noqa: PLC0415

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    user_id: str = payload.get("sub", "")

    # SSRF check if webhook_url is being changed
    if body.webhook_url is not None:
        ssrf_err = validate_url(body.webhook_url)
        if ssrf_err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsafe webhook URL: {ssrf_err}",
            )
        s = get_settings()
        if s.app_env == "production" and not body.webhook_url.startswith("https://"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Webhook URL must use HTTPS in production.",
            )

    kwargs: dict[str, Any] = {}
    _updatable = (
        "description", "webhook_url", "webhook_method",
        "auth_header_name", "auth_header_value",
        "timeout_seconds", "is_active",
    )
    for field_name in _updatable:
        val = getattr(body, field_name, None)
        if val is not None:
            kwargs[field_name] = val

    if not kwargs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No fields to update.",
        )

    tool = await update_org_tool(tool_id, org_id, actor_id=user_id, **kwargs)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found.")

    await invalidate_org_tools_cache(org_id)
    return JSONResponse(content=_org_tool_to_response(tool))


@app.delete("/tools/{tool_id}", tags=["Tools"])
async def remove_tool(
    tool_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Soft-delete a custom tool."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    user_id: str = payload.get("sub", "")
    deleted = await delete_org_tool(tool_id, org_id, actor_id=user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found.")
    await invalidate_org_tools_cache(org_id)
    return JSONResponse(content={"status": "deleted"})


@app.get("/admin/tools/{org_id}", tags=["Admin"])
async def admin_list_tools(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list all custom tools for a given org (including inactive)."""
    tools = await list_org_tools(org_id, active_only=False)
    return JSONResponse(content=[_org_tool_to_response(t) for t in tools])


# ─── Memory endpoints ────────────────────────────────────────────────────────


class StoreMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)


@app.get("/memories", tags=["Memory"])
async def list_memories_endpoint(
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="ISO datetime cursor for pagination"),
) -> JSONResponse:
    """List memories for the authenticated org (newest first, cursor-paginated)."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — memory requires an org-scoped credential.",
        )

    from datetime import datetime as _dt

    cursor_dt = _dt.fromisoformat(cursor) if cursor else None
    entries = await list_memories(org_id, limit, cursor_dt)
    serialized = [
        {
            "id": e.id,
            "content": e.content,
            "source_run_id": e.source_run_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    total = await count_memories(org_id)
    return JSONResponse(
        content={"items": serialized, "total": total, "next_cursor": next_cursor}
    )


@app.post("/memories", tags=["Memory"])
async def store_memory_endpoint(
    body: StoreMemoryRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Manually store a memory for the authenticated org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — memory requires an org-scoped credential.",
        )
    user_id: str = payload.get("sub", "")

    entry = await store_memory(org_id, user_id, body.content)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Failed to store memory — org limit may have been reached.",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": entry.id,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
        },
    )


@app.delete("/memories/{memory_id}", tags=["Memory"])
async def delete_memory_endpoint(
    memory_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Delete a specific memory (org-scoped)."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No org_id in token — memory requires an org-scoped credential.",
        )
    deleted = await delete_memory(memory_id, org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found.")
    return JSONResponse(content={"status": "deleted"})


@app.get("/admin/memories/org/{org_id}", tags=["Admin"])
async def admin_list_org_memories(
    org_id: str,
    _admin: dict = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Admin: list memories for a specific org."""
    entries = await list_memories(org_id, limit)
    total = await count_memories(org_id)
    serialized = [
        {
            "id": e.id,
            "content": e.content,
            "user_id": e.user_id,
            "source_run_id": e.source_run_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]
    return JSONResponse(content={"items": serialized, "total": total})


@app.delete("/admin/memories/org/{org_id}", tags=["Admin"])
async def admin_purge_org_memories(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: delete all memories for a specific org."""
    deleted_count = await delete_all_org_memories(org_id)
    return JSONResponse(content={"status": "purged", "deleted": deleted_count})


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


@app.post("/mcp/servers", tags=["MCP"])
async def create_mcp_server(
    body: CreateMcpServerRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Register an external MCP server for the authenticated org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
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


@app.get("/mcp/servers", tags=["MCP"])
async def list_mcp_servers(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List MCP servers for the authenticated org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    servers = await list_org_mcp_servers(org_id)
    return JSONResponse(content=[_mcp_server_to_response(s) for s in servers])


@app.get("/mcp/servers/{server_id}", tags=["MCP"])
async def get_mcp_server(
    server_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get a specific MCP server by ID."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    srv = await get_org_mcp_server(server_id, org_id)
    if srv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")
    return JSONResponse(content=_mcp_server_to_response(srv))


@app.patch("/mcp/servers/{server_id}", tags=["MCP"])
async def patch_mcp_server(
    server_id: str,
    body: UpdateMcpServerRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Update an MCP server (partial update)."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    user_id: str = payload.get("sub", "")

    kwargs: dict[str, Any] = {}
    for field_name in ("name", "url", "auth_type", "auth_token", "auth_header_name", "timeout_seconds", "is_active"):
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


@app.delete("/mcp/servers/{server_id}", tags=["MCP"])
async def remove_mcp_server(
    server_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Soft-delete an MCP server."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    user_id: str = payload.get("sub", "")
    deleted = await delete_org_mcp_server(server_id, org_id, actor_id=user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found.")
    return JSONResponse(content={"status": "deleted"})


@app.post("/mcp/servers/{server_id}/discover", tags=["MCP"])
async def discover_mcp_server_tools(
    server_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Connect to an MCP server and return its available tools."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
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


@app.get("/admin/mcp/servers/{org_id}", tags=["Admin"])
async def admin_list_mcp_servers(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list all MCP servers for an org."""
    servers = await list_org_mcp_servers(org_id, active_only=False)
    return JSONResponse(content=[_mcp_server_to_response(s) for s in servers])


# ─── Entry point for `python app.py` ─────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.app_log_level,
        reload=settings.app_env == "development",
    )
