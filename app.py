# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Teardrop FastAPI application.

Endpoints
---------
GET  /                       – redirect to /docs
GET  /health                 – health check
POST /token                  – tri-mode auth (client-creds, email+secret, SIWE)
POST /register               – self-serve org + user registration
GET  /auth/verify-email      – verify email address via one-time token
POST /auth/resend-verification – resend verification email
POST /auth/refresh           – exchange refresh token for new access + refresh tokens
POST /auth/logout            – revoke refresh token
POST /org/invite             – create an org invite link
POST /register/invite        – accept an org invite and create account
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
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent.graph import close_checkpointer, get_graph, init_checkpointer
from agent.state import AgentState
from agent_wallets import (
    close_agent_wallets_db,
    create_agent_wallet,
    deactivate_agent_wallet,
    get_agent_wallet,
    get_agent_wallet_balance,
    init_agent_wallets_db,
)
from auth import create_access_token, require_auth
from benchmarks import (
    build_benchmarks_response,
    close_benchmarks_db,
    init_benchmarks_db,
)
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
    enqueue_failed_settlement,
    get_billing_history,
    get_byok_platform_fee,
    get_credit_balance,
    get_credit_history,
    get_current_pricing,
    get_invoice_by_run,
    get_invoices,
    get_org_spending_config,
    get_pending_settlements,
    get_revenue_summary,
    get_stripe_session_status,
    get_tool_pricing_overrides,
    handle_stripe_webhook,
    init_billing,
    process_pending_settlements,
    record_settlement,
    reset_exhausted_settlement,
    settle_payment,
    update_org_spending_config,
    upsert_tool_pricing_override,
    verify_and_settle_usdc_topup,
    verify_credit,
    verify_payment,
)
from cache import close_redis, get_redis, init_redis
from config import Settings, get_settings
from mcp_client import (
    OrgMcpServer,
    build_mcp_langchain_tools,
    close_mcp_client_db,
    create_org_mcp_server,
    delete_org_mcp_server,
    discover_mcp_tools,
    get_org_mcp_server,
    init_mcp_client_db,
    list_org_mcp_servers,
    update_org_mcp_server,
)
from memory import (
    cleanup_expired_memories,
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
from email_utils import send_invite_email, send_verification_email
from llm_config import (
    ALLOWED_ROUTING_PREFERENCES,
    OrgLlmConfig,
    build_llm_config_dict,
    close_llm_config_db,
    delete_org_llm_config,
    get_org_llm_config,
    get_org_llm_config_cached,
    init_llm_config_db,
    invalidate_llm_config_cache,
    resolve_llm_config,
    upsert_org_llm_config,
)
from marketplace import (
    check_org_subscription,
    close_marketplace_db,
    complete_withdrawal,
    get_author_balance,
    get_author_config,
    get_author_earnings_history,
    get_marketplace_catalog,
    get_marketplace_tool_by_name,
    init_marketplace_db,
    list_pending_withdrawals,
    process_withdrawal,
    record_tool_call_earnings,
    request_withdrawal,
    set_author_config,
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
    list_marketplace_tools,
    list_org_tools,
    update_org_tool,
)
from scripts.generate_keys import generate_keypair
from tools import registry
from tools.mcp_server import mcp as _mcp_server
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
    consume_org_invite,
    consume_refresh_token,
    consume_verification_token,
    create_client_credential,
    create_org,
    create_org_invite,
    create_refresh_token,
    create_user,
    create_verification_token,
    get_client_credential_by_id,
    get_org_by_name,
    get_org_invite,
    get_user_by_email,
    get_user_by_org_id,
    init_user_db,
    mark_user_verified,
    register_org_and_user,
    revoke_refresh_token,
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

    # Marketplace requires billing to be enabled — without billing, tool calls are
    # free but earnings are still recorded, creating uncollectable phantom entries.
    if s.marketplace_enabled and not s.billing_enabled:
        msg = (
            "MARKETPLACE_ENABLED=true requires BILLING_ENABLED=true. "
            "Without billing, callers are not charged but author earnings are recorded, "
            "creating phantom ledger entries that can never be collected."
        )
        if is_prod:
            raise RuntimeError(msg)
        logger.warning("%s ⚠  %s", prefix, msg)

    # Marketplace requires the org tool encryption key for webhook auth decryption.
    if s.marketplace_enabled and not s.org_tool_encryption_key:
        msg = (
            "MARKETPLACE_ENABLED=true requires ORG_TOOL_ENCRYPTION_KEY to be set. "
            "Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
        if is_prod:
            raise RuntimeError(msg)
        logger.warning("%s ⚠  %s", prefix, msg)

    # Log a concise summary so operators can see the active config at a glance.
    logger.info(
        "%s   env=%s billing=%s cors=%s siwe_domain=%s",
        prefix,
        s.app_env,
        s.billing_enabled,
        s.cors_origins or "*(open)",
        s.siwe_domain or "(app_host fallback)",
    )


async def _settlement_retry_loop() -> None:
    """Periodically retry failed settlements (runs as background task)."""
    interval = settings.settlement_retry_interval_seconds
    while True:
        try:
            await asyncio.sleep(interval)
            processed = await process_pending_settlements()
            if processed:
                logger.info("Settlement retry: processed %d pending settlements", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Settlement retry loop error")


async def _memory_cleanup_loop() -> None:
    """Periodically delete expired memories (runs as background task)."""
    interval = settings.memory_cleanup_interval_seconds
    while True:
        try:
            await asyncio.sleep(interval)
            deleted = await cleanup_expired_memories()
            if deleted:
                logger.info("Memory cleanup: deleted %d expired memories", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Memory cleanup loop error")


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
    await init_marketplace_db(pool)
    await init_llm_config_db(pool)
    await init_benchmarks_db(pool)
    await init_agent_wallets_db(pool)

    # Launch background workers.
    bg_tasks: list[asyncio.Task] = []
    if settings.billing_enabled:
        bg_tasks.append(asyncio.create_task(_settlement_retry_loop()))
    if settings.memory_enabled and settings.memory_ttl_days > 0:
        bg_tasks.append(asyncio.create_task(_memory_cleanup_loop()))
    if settings.marketplace_auto_sweep_enabled:
        from marketplace import _marketplace_sweep_loop

        bg_tasks.append(asyncio.create_task(_marketplace_sweep_loop()))

    yield

    # Cancel background workers.
    for task in bg_tasks:
        task.cancel()
    for task in bg_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass

    await close_agent_wallets_db()
    await close_benchmarks_db()
    await close_llm_config_db()
    await close_marketplace_db()
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
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Payment-Signature", "X-Payment"],
)

# ─── MCP gateway (auth / billing / x402 — wraps FastMCP ASGI app) ────────────
from mcp_gateway import MCPGatewayMiddleware  # noqa: E402

app.add_middleware(MCPGatewayMiddleware)

# ─── MCP Streamable HTTP endpoint (Smithery / direct MCP clients) ────────────
app.mount("/tools/mcp", _mcp_server.http_app())

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
            "version": app.version,
            "environment": settings.app_env,
            "postgres": postgres,
            "redis": redis_status,
        }
    )


@app.get("/.well-known/jwks.json", tags=["System"])
async def jwks() -> JSONResponse:
    """Expose the RS256 public key in JWKS format for external JWT verification."""
    import base64

    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(settings.jwt_public_key.encode())
    nums = pub.public_numbers()  # type: ignore[union-attr]

    def _b64url(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    return JSONResponse(content={
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "teardrop-rs256",
            "n": _b64url(nums.n),
            "e": _b64url(nums.e),
        }],
    })


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
                    **({
                        "max_amount": settings.x402_upto_max_amount,
                    } if settings.x402_scheme == "upto" else {}),
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


@app.get("/.well-known/mcp/server-card.json", tags=["MCP"])
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
                tools.append({
                    "name": mt.name,
                    "description": mt.marketplace_description or mt.description,
                    "inputSchema": mt.input_schema,
                })
        except Exception:
            logger.debug("Failed to load marketplace tools for server card", exc_info=True)
    return JSONResponse(
        content={
            "serverInfo": {"name": "teardrop-tools", "version": app.version},
            "authentication": {"required": True, "schemes": ["bearer"]},
            "tools": tools,
            "resources": [],
            "prompts": [],
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
        if settings.require_email_verification and not user.is_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Please verify your email before signing in.",
            )
        extra_claims = {
            "org_id": user.org_id,
            "email": user.email,
            "role": user.role,
            "auth_method": "email",
        }
        access_token = create_access_token(subject=user.id, extra_claims=extra_claims)
        refresh_token = await create_refresh_token(
            user_id=user.id,
            org_id=user.org_id,
            auth_method="email",
            extra_claims=extra_claims,
            expire_days=settings.refresh_token_expire_days,
        )
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.jwt_access_token_expire_minutes * 60,
                "refresh_token": refresh_token,
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
        return JSONResponse(content=await _handle_siwe_login(body.siwe_message, body.siwe_signature))

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Provide email+secret, client_id+client_secret, or siwe_message+siwe_signature.",
    )


async def _handle_siwe_login(siwe_message: str, siwe_signature: str) -> dict:
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

    # Verify EIP-191 signature BEFORE consuming nonce.
    # This prevents nonce-exhaustion DoS: an invalid signature must never burn
    # a legitimate nonce. The nonce is embedded in the signed SIWE message, so
    # an attacker cannot forge a valid signature for someone else's nonce.
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

    # Consume nonce AFTER signature verification (single-use + TTL + address binding)
    if not await consume_nonce(msg.nonce, settings.siwe_nonce_ttl_seconds, expected_address=address):
        raise HTTPException(status_code=401, detail="Invalid or expired nonce")
    logger.info("SIWE nonce consumed for address=%s chain=%d", address, chain_id)

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

    siwe_claims = {
        "org_id": wallet.org_id,
        "address": address,
        "chain_id": chain_id,
        "auth_method": "siwe",
        "role": "user",
        "email": f"{address.lower()}@wallet",
    }
    access_token = create_access_token(subject=wallet.user_id, extra_claims=siwe_claims)
    siwe_refresh = await create_refresh_token(
        user_id=wallet.user_id,
        org_id=wallet.org_id,
        auth_method="siwe",
        extra_claims=siwe_claims,
        expire_days=settings.refresh_token_expire_days,
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.jwt_access_token_expire_minutes * 60,
        "refresh_token": siwe_refresh,
    }


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


# ─── Self-serve registration & email verification ──────────────────────────────


class RegisterRequest(BaseModel):
    org_name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)


@app.post("/register", tags=["Auth"], status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request) -> JSONResponse:
    """Self-serve org and user registration. Returns a JWT immediately.

    A verification email is sent when RESEND_API_KEY is configured.
    Set REQUIRE_EMAIL_VERIFICATION=true to gate /token login until verified.
    """
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
    try:
        org, user = await register_org_and_user(
            org_name=body.org_name,
            email=body.email,
            secret=body.password,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email or organisation name already exists.",
        )
    verification_token = await create_verification_token(user.id)
    asyncio.create_task(
        send_verification_email(user.email, verification_token, settings.app_base_url)
    )
    extra_claims = {
        "org_id": user.org_id,
        "email": user.email,
        "role": user.role,
        "auth_method": "email",
    }
    access_token = create_access_token(subject=user.id, extra_claims=extra_claims)
    refresh_token = await create_refresh_token(
        user_id=user.id,
        org_id=user.org_id,
        auth_method="email",
        extra_claims=extra_claims,
        expire_days=settings.refresh_token_expire_days,
    )
    logger.info("register org=%s user=%s", org.id, user.id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.jwt_access_token_expire_minutes * 60,
            "refresh_token": refresh_token,
        },
    )


@app.get("/auth/verify-email", tags=["Auth"])
async def verify_email(token: str = Query(...)) -> JSONResponse:
    """Verify an email address via a one-time token."""
    user_id = await consume_verification_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Verification token is invalid, expired, or already used.",
        )
    await mark_user_verified(user_id)
    return JSONResponse(content={"verified": True})


class ResendVerificationRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


@app.post("/auth/resend-verification", tags=["Auth"])
async def resend_verification(body: ResendVerificationRequest, request: Request) -> JSONResponse:
    """Re-send a verification email. Always returns 200 to prevent email oracle attacks."""
    client_ip = request.client.host if request.client else "unknown"
    resend_limit = max(1, settings.rate_limit_auth_rpm // 4)
    allowed, remaining, reset_at = await _check_rate_limit(f"resend:{client_ip}", resend_limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={
                "X-RateLimit-Limit": str(resend_limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": "60",
            },
        )
    user = await get_user_by_email(body.email)
    if user is not None and not user.is_verified:
        verification_token = await create_verification_token(user.id)
        asyncio.create_task(
            send_verification_email(user.email, verification_token, settings.app_base_url)
        )
    return JSONResponse(
        content={"message": "If that email is registered, a verification link has been sent."}
    )


# ─── Refresh tokens ────────────────────────────────────────────────────────────


class RefreshRequest(BaseModel):
    refresh_token: str


@app.post("/auth/refresh", tags=["Auth"])
async def refresh_token_endpoint(body: RefreshRequest, request: Request) -> JSONResponse:
    """Exchange a refresh token for a new access token and rotated refresh token."""
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
    record = await consume_refresh_token(body.refresh_token)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked refresh token.",
        )
    access_token = create_access_token(subject=record.user_id, extra_claims=record.extra_claims)
    new_refresh = await create_refresh_token(
        user_id=record.user_id,
        org_id=record.org_id,
        auth_method=record.auth_method,
        extra_claims=record.extra_claims,
        expire_days=settings.refresh_token_expire_days,
    )
    return JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.jwt_access_token_expire_minutes * 60,
            "refresh_token": new_refresh,
        }
    )


class LogoutRequest(BaseModel):
    refresh_token: str


@app.post("/auth/logout", tags=["Auth"])
async def logout(
    body: LogoutRequest,
    payload: dict = Depends(require_auth),
) -> Response:
    """Revoke a refresh token (logout)."""
    await revoke_refresh_token(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Org invites ───────────────────────────────────────────────────────────────


class CreateInviteRequest(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    role: str = "user"


@app.post("/org/invite", tags=["Auth"], status_code=status.HTTP_201_CREATED)
async def create_invite(
    body: CreateInviteRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Create an org invite link. Any authenticated org member may invite."""
    org_id = payload.get("org_id", "")
    user_id = payload["sub"]
    invite = await create_org_invite(
        org_id=org_id,
        invited_by=user_id,
        email=body.email,
        role=body.role,
    )
    invite_url = (
        f"{settings.app_base_url.rstrip('/')}/register/invite?token={invite.token}"
        if settings.app_base_url
        else None
    )
    if body.email:
        asyncio.create_task(
            send_invite_email(body.email, invite.token, org_id, settings.app_base_url)
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "token": invite.token,
            "invite_url": invite_url,
            "expires_at": invite.expires_at.isoformat(),
        },
    )


class AcceptInviteRequest(BaseModel):
    token: str
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)


@app.post("/register/invite", tags=["Auth"], status_code=status.HTTP_201_CREATED)
async def register_via_invite(body: AcceptInviteRequest, request: Request) -> JSONResponse:
    """Accept an org invite token and create a new user account."""
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
    invite = await get_org_invite(body.token)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Invite token is invalid, expired, or already used.",
        )
    # Enforce email match if the invite was issued for a specific address.
    if invite.email and invite.email.lower() != body.email.lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This invite was sent to a different email address.",
        )
    consumed = await consume_org_invite(body.token)
    if not consumed:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Invite token is invalid, expired, or already used.",
        )
    try:
        user = await create_user(
            email=body.email,
            secret=body.password,
            org_id=invite.org_id,
            role=invite.role,
            is_verified=True,  # accepting the invite is the trust signal
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        )
    extra_claims = {
        "org_id": user.org_id,
        "email": user.email,
        "role": user.role,
        "auth_method": "email",
    }
    access_token = create_access_token(subject=user.id, extra_claims=extra_claims)
    refresh_token = await create_refresh_token(
        user_id=user.id,
        org_id=user.org_id,
        auth_method="email",
        extra_claims=extra_claims,
        expire_days=settings.refresh_token_expire_days,
    )
    logger.info("register_via_invite org=%s user=%s", user.org_id, user.id)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.jwt_access_token_expire_minutes * 60,
            "refresh_token": refresh_token,
        },
    )


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

    # ── Per-org aggregate rate limit ────────────────────────────────────────
    # Guards against a single org saturating the LLM pool across many users.
    org_rpm: int = settings.rate_limit_org_agent_rpm
    if org_id and isinstance(org_rpm, int):
        org_allowed, org_remaining, org_reset_at = await _check_rate_limit(
            f"run:org:{org_id}", org_rpm
        )
        if not org_allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Organization rate limit exceeded. Please slow down.",
                headers={
                    "X-RateLimit-Limit": str(org_rpm),
                    "X-RateLimit-Remaining": str(org_remaining),
                    "X-RateLimit-Reset": str(org_reset_at),
                    "Retry-After": "60",
                    "X-RateLimit-Scope": "org",
                },
            )

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
    #   siwe with balance → org prepaid credit (verify_credit, bills actual cost)
    #   siwe no balance   → x402 on-chain payment header (verify_payment, exact)
    #   client_credentials / email → org prepaid credit balance (verify_credit)
    #
    # Note: the siwe-with-balance path is the upto workaround — SIWE users who
    # top up via /billing/topup/usdc are billed calculate_run_cost_usdc() actual
    # cost post-run rather than the fixed x402 exact price.
    #
    # BYOK orgs are charged a flat platform fee instead of LLM cost.
    billing = BillingResult()
    auth_method = payload.get("auth_method", "")

    # Resolve BYOK status early so the billing gate uses the right minimum.
    _org_llm_cfg = await get_org_llm_config_cached(org_id)
    is_byok = _org_llm_cfg.is_byok if _org_llm_cfg else False
    platform_fee = get_byok_platform_fee(is_byok)

    if settings.billing_enabled and auth_method in settings.billable_auth_methods:
        if auth_method == "siwe":
            # Prefer credit billing when the org has a prepaid balance.
            siwe_credit_balance = await get_credit_balance(org_id)
            if siwe_credit_balance > 0:
                pricing = await get_current_pricing()
                default_min = pricing.run_price_usdc if pricing is not None else 0
                min_required = platform_fee if is_byok else default_min
                billing = await verify_credit(org_id, min_required)
                if not billing.verified:
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail=billing.error,
                    )
            else:
                # No credit balance: require per-request x402 exact payment.
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
            default_min = pricing.run_price_usdc if pricing is not None else 0
            min_required = platform_fee if is_byok else default_min
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

        # ── Merge subscribed marketplace tools ────────────────────────────
        mp_by_name: dict[str, Any] = {}
        try:
            from marketplace import build_subscribed_marketplace_tools

            mp_tools, mp_by_name = await build_subscribed_marketplace_tools(org_id)
            org_lc_tools = list(org_lc_tools) + mp_tools
            org_tools_by_name = {**org_tools_by_name, **mp_by_name}
        except Exception:
            logger.debug("Marketplace subscription injection failed for org_id=%s", org_id, exc_info=True)

        # ── Recall relevant memories for this org ─────────────────────────
        recalled: list[str] = []
        mem_settings = get_settings()
        if mem_settings.memory_enabled:
            try:
                entries = await recall_memories(org_id, body.message, mem_settings.memory_top_k)
                recalled = [e.content for e in entries]
            except Exception:
                logger.debug("Memory recall failed for org_id=%s", org_id, exc_info=True)

        # ── Resolve per-org LLM config (smart routing aware) ────────────
        llm_config: dict | None = None
        try:
            llm_config = await resolve_llm_config(org_id)
        except Exception:
            logger.debug("LLM config resolution failed for org_id=%s; using global default", org_id, exc_info=True)

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
                "_llm_config": llm_config,
                "_db_pool": request.app.state.pool,
                "_jwt_token": (request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None),
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
            _run_provider = llm_config["provider"] if llm_config else settings.agent_provider
            _run_model = llm_config["model"] if llm_config else settings.agent_model
            cost_usdc = await calculate_run_cost_usdc(usage_data, _run_provider, _run_model)
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
            platform_fee_usdc=platform_fee,
            provider=llm_config["provider"] if llm_config else settings.agent_provider,
            model=llm_config["model"] if llm_config else settings.agent_model,
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
        delegation_spend = usage_data.get("delegation_spend_usdc", 0)

        if billing.verified:
            # For BYOK orgs, debit only the platform fee (they pay the LLM
            # provider directly).  For non-BYOK, debit the full LLM cost
            # (platform_fee is 0 in that case, so debit_amount == cost_usdc).
            debit_amount = platform_fee if is_byok else cost_usdc

            if billing.billing_method == "credit":
                # Debit actual run cost (or platform fee for BYOK) from org's prepaid balance.
                success = await debit_credit(org_id, debit_amount, reason=f"run:{run_id}")
                if success:
                    await record_settlement(usage_event.id, debit_amount, "", "settled")
                    yield _sse_event(
                        _EV_BILLING_SETTLEMENT,
                        {
                            "run_id": run_id,
                            "amount_usdc": debit_amount,
                            "tx_hash": "",
                            "network": "credit",
                            "delegation_cost_usdc": delegation_spend,
                            "platform_fee_usdc": platform_fee,
                        },
                    )
                else:
                    await record_settlement(usage_event.id, debit_amount, "", "failed")
                    await enqueue_failed_settlement(
                        usage_event.id, org_id, run_id, "credit", debit_amount,
                    )
                    logger.warning("Credit debit failed run_id=%s org_id=%s", run_id, org_id)
            else:
                # x402 on-chain settlement.
                billing_settled = await settle_payment(
                    billing, actual_cost_usdc=cost_usdc,
                )
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
                            "delegation_cost_usdc": delegation_spend,
                            "platform_fee_usdc": platform_fee,
                        },
                    )
                else:
                    await record_settlement(usage_event.id, 0, "", "failed")
                    await enqueue_failed_settlement(
                        usage_event.id, org_id, run_id, "x402", cost_usdc,
                        payment_payload=str(billing.payment_payload) if billing.payment_payload else None,
                    )
                    logger.warning(
                        "Settlement failed run_id=%s: %s",
                        run_id,
                        billing_settled.error,
                    )

        # ── Record marketplace tool earnings for subscribed tools ────────
        try:
            tool_names_used = usage_data.get("tool_names", [])
            if mp_by_name and tool_names_used:
                overrides = await get_tool_pricing_overrides()
                pricing = await get_current_pricing()
                default_cost = pricing.tool_call_cost if pricing else 0

                for tname in tool_names_used:
                    if tname in mp_by_name and "/" in tname:
                        t_slug, t_bare = tname.split("/", 1)
                        t_row = await get_marketplace_tool_by_name(t_bare, t_slug)
                        if t_row:
                            author_price = t_row.get("base_price_usdc", 0)
                            t_cost = overrides.get(tname, overrides.get(t_bare, author_price or default_cost))
                            author_oid = t_row.get("org_id")
                            if author_oid and t_cost > 0:
                                asyncio.create_task(
                                    record_tool_call_earnings(
                                        author_org_id=author_oid,
                                        caller_org_id=org_id,
                                        tool_name=t_bare,
                                        total_cost_usdc=t_cost,
                                    )
                                )
        except Exception:
            logger.debug("Marketplace earnings recording failed", exc_info=True)

        yield _sse_event(
            _EV_USAGE_SUMMARY,
            {
                "run_id": run_id,
                "tokens_in": usage_event.tokens_in,
                "tokens_out": usage_event.tokens_out,
                "tool_calls": usage_event.tool_calls,
                "duration_ms": usage_event.duration_ms,
                "cost_usdc": usage_event.cost_usdc,
                "platform_fee_usdc": platform_fee,
                "delegation_cost_usdc": delegation_spend,
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

    # Verify signature BEFORE consuming nonce (prevent nonce-exhaustion DoS)
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

    # Consume nonce AFTER signature verification (single-use + TTL + address binding)
    if not await consume_nonce(msg.nonce, settings.siwe_nonce_ttl_seconds, expected_address=address):
        raise HTTPException(status_code=401, detail="Invalid or expired nonce")
    logger.info("SIWE nonce consumed for address=%s chain=%d", address, chain_id)

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


# ─── Agent Wallet endpoints ──────────────────────────────────────────────────


@app.post("/wallets/agent", tags=["Agent Wallets"])
async def provision_agent_wallet(
    payload: dict = Depends(require_auth),
    chain_id: int | None = None,
) -> JSONResponse:
    """Provision a CDP-backed agent wallet for the caller's org."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise HTTPException(status_code=501, detail="Agent wallets are not enabled")
    try:
        wallet = await create_agent_wallet(
            org_id=payload.get("org_id", ""),
            actor_id=payload["sub"],
            chain_id=chain_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": wallet.id,
            "address": wallet.address,
            "chain_id": wallet.chain_id,
            "wallet_type": wallet.wallet_type,
            "is_active": wallet.is_active,
            "created_at": wallet.created_at.isoformat(),
        },
    )


@app.get("/wallets/agent", tags=["Agent Wallets"])
async def get_agent_wallet_info(
    payload: dict = Depends(require_auth),
    chain_id: int | None = None,
    include_balance: bool = False,
) -> JSONResponse:
    """Return the org's agent wallet, optionally including on-chain USDC balance."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise HTTPException(status_code=501, detail="Agent wallets are not enabled")
    wallet = await get_agent_wallet(
        org_id=payload.get("org_id", ""),
        chain_id=chain_id,
    )
    if wallet is None:
        raise HTTPException(status_code=404, detail="No agent wallet found for this org")
    result: dict = {
        "id": wallet.id,
        "address": wallet.address,
        "chain_id": wallet.chain_id,
        "wallet_type": wallet.wallet_type,
        "is_active": wallet.is_active,
        "created_at": wallet.created_at.isoformat(),
    }
    if include_balance:
        try:
            balance_info = await get_agent_wallet_balance(
                org_id=payload.get("org_id", ""),
                chain_id=chain_id,
            )
            result["balance_usdc"] = balance_info["balance_usdc"]
        except Exception:
            result["balance_usdc"] = None
            result["balance_error"] = "Failed to fetch on-chain balance"
    return JSONResponse(content=result)


@app.delete("/wallets/agent", tags=["Agent Wallets"])
async def deactivate_org_agent_wallet(
    _admin: dict = Depends(require_admin),
    chain_id: int | None = None,
) -> JSONResponse:
    """Deactivate the org's agent wallet (admin only)."""
    settings = get_settings()
    if not settings.agent_wallet_enabled:
        raise HTTPException(status_code=501, detail="Agent wallets are not enabled")
    deactivated = await deactivate_agent_wallet(
        org_id=_admin.get("org_id", ""),
        actor_id=_admin["sub"],
        chain_id=chain_id,
    )
    if not deactivated:
        raise HTTPException(status_code=404, detail="No active agent wallet found")
    return JSONResponse(content={"status": "deactivated"})


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

    Accepts built-in tool names or qualified marketplace names (e.g. 'acme/weather').
    """
    known_names = {t.name for t in registry.list_latest(include_deprecated=True)}
    tool_valid = body.tool_name in known_names

    # Also accept qualified marketplace tool names
    if not tool_valid and "/" in body.tool_name:
        slug, tname = body.tool_name.split("/", 1)
        mp_tool = await get_marketplace_tool_by_name(tname, slug)
        tool_valid = mp_tool is not None

    if not tool_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown tool name: {body.tool_name!r}. Must be a registered tool or qualified marketplace name.",
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
    spending = await get_org_spending_config(org_id)
    return JSONResponse(content={
        "org_id": org_id,
        "balance_usdc": balance,
        "spending_limit_usdc": spending["spending_limit_usdc"],
        "is_paused": spending["is_paused"],
        "daily_spend_usdc": spending["daily_spend_usdc"],
    })


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


@app.get("/admin/billing/pending", tags=["Admin"])
async def admin_billing_pending(
    _admin: dict = Depends(require_admin),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = 50,
) -> JSONResponse:
    """List pending/failed settlements for reconciliation (admin only)."""
    rows = await get_pending_settlements(status_filter, min(limit, 200))
    serialized = []
    for r in rows:
        row = dict(r)
        for key in ("next_retry_at", "created_at"):
            if key in row and row[key] is not None:
                row[key] = row[key].isoformat()
        serialized.append(row)
    return JSONResponse(content={"items": serialized})


@app.post("/admin/billing/pending/{settlement_id}/retry", tags=["Admin"])
async def admin_billing_retry(
    settlement_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Reset an exhausted settlement for manual retry (admin only)."""
    ok = await reset_exhausted_settlement(settlement_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Settlement not found or not in 'exhausted' status",
        )
    return JSONResponse(content={"settlement_id": settlement_id, "status": "pending"})


class SpendingConfigUpdate(BaseModel):
    spending_limit_usdc: int | None = None
    is_paused: bool | None = None


@app.get("/admin/orgs/{org_id}/spending", tags=["Admin"])
async def admin_get_spending(
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Get spending configuration for an org (admin only)."""
    config = await get_org_spending_config(org_id)
    return JSONResponse(content=config)


@app.patch("/admin/orgs/{org_id}/spending", tags=["Admin"])
async def admin_update_spending(
    org_id: str,
    body: SpendingConfigUpdate,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Update spending limit or pause/unpause an org (admin only)."""
    result = await update_org_spending_config(
        org_id,
        spending_limit_usdc=body.spending_limit_usdc,
        is_paused=body.is_paused,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Org not found in credit system",
        )
    return JSONResponse(content=result)


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
        description=(
            "Amount in atomic USDC (6 decimals)."
            " Min $1.00 = 1_000_000. Max $10,000 = 10_000_000_000."
        ),
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
    publish_as_mcp: bool = False
    marketplace_description: str | None = Field(default=None, max_length=1000)
    base_price_usdc: int = Field(default=0, ge=0, le=100_000_000)


class UpdateOrgToolRequest(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=2048)
    webhook_method: str | None = Field(default=None, pattern=r"^(GET|POST|PUT)$")
    auth_header_name: str | None = None
    auth_header_value: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)
    is_active: bool | None = None
    publish_as_mcp: bool | None = None
    marketplace_description: str | None = Field(default=None, max_length=1000)
    base_price_usdc: int | None = Field(default=None, ge=0, le=100_000_000)


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
    publish_as_mcp: bool
    marketplace_description: str
    base_price_usdc: int
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
        "publish_as_mcp": tool.publish_as_mcp,
        "marketplace_description": tool.marketplace_description,
        "base_price_usdc": tool.base_price_usdc,
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
            publish_as_mcp=body.publish_as_mcp,
            marketplace_description=body.marketplace_description or "",
            base_price_usdc=body.base_price_usdc,
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
        "publish_as_mcp", "marketplace_description",
        "base_price_usdc",
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


# ─── LLM Config endpoints ────────────────────────────────────────────────────


class UpsertLlmConfigRequest(BaseModel):
    provider: str = Field(..., description="LLM provider: anthropic, openai, or google")
    model: str = Field(..., min_length=1, max_length=200, description="Model identifier")
    api_key: str | None = Field(
        default=None,
        description="Provider API key (BYOK). Omit to use Teardrop shared key.",
    )
    api_base: str | None = Field(
        default=None,
        description="Custom base URL for OpenAI-compatible endpoints (vLLM, Ollama, etc.)",
    )
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    routing_preference: str = Field(default="default", description="default, cost, speed, or quality")


def _llm_config_to_response(cfg: OrgLlmConfig) -> dict:
    return {
        "org_id": cfg.org_id,
        "provider": cfg.provider,
        "model": cfg.model,
        "has_api_key": cfg.has_api_key,
        "api_base": cfg.api_base,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "timeout_seconds": cfg.timeout_seconds,
        "routing_preference": cfg.routing_preference,
        "is_byok": cfg.is_byok,
        "created_at": cfg.created_at.isoformat(),
        "updated_at": cfg.updated_at.isoformat(),
    }


@app.get("/llm-config", tags=["LLM Config"])
async def get_llm_config_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the authenticated org's LLM configuration."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    cfg = await get_org_llm_config(org_id)
    if cfg is None:
        return JSONResponse(
            content={
                "configured": False,
                "provider": settings.agent_provider,
                "model": settings.agent_model,
            }
        )
    return JSONResponse(content={"configured": True, **_llm_config_to_response(cfg)})


@app.put("/llm-config", tags=["LLM Config"])
async def upsert_llm_config_endpoint(
    body: UpsertLlmConfigRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Create or update the authenticated org's LLM configuration."""
    from agent.llm import ALLOWED_PROVIDERS
    from tools.definitions.http_fetch import validate_url

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    if body.provider.lower() not in ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid provider '{body.provider}'. Allowed: {', '.join(sorted(ALLOWED_PROVIDERS))}",
        )

    if body.routing_preference not in ALLOWED_ROUTING_PREFERENCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid routing_preference. Allowed: {', '.join(sorted(ALLOWED_ROUTING_PREFERENCES))}",
        )

    # Provider-specific temperature limits
    _provider_temp_limits: dict[str, float] = {"anthropic": 1.0, "openai": 2.0, "google": 2.0}
    temp_limit = _provider_temp_limits.get(body.provider.lower(), 2.0)
    if body.temperature > temp_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Provider '{body.provider}' requires temperature ≤ {temp_limit}",
        )

    # Non-BYOK model name validation against known catalogue
    if body.api_key is None:
        from benchmarks import MODEL_CATALOGUE

        catalogue_key = f"{body.provider.lower()}:{body.model}"
        if catalogue_key not in MODEL_CATALOGUE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unknown model '{body.model}' for provider '{body.provider}'. "
                    "Supply an api_key (BYOK) to use custom/fine-tuned models."
                ),
            )

    # SSRF validation for api_base
    if body.api_base:
        if body.provider.lower() != "openai":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="api_base is only supported for provider 'openai' (OpenAI-compatible endpoints).",
            )
        # Always validate URL structure and scheme
        ssrf_err = validate_url(body.api_base)
        if ssrf_err:
            # When private endpoints are allowed, only reject non-http(s) schemes
            # and DNS failures — allow private IPs
            if not settings.allow_private_llm_endpoints:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsafe api_base URL: {ssrf_err}",
                )
            elif "Blocked scheme" in ssrf_err or "DNS resolution failed" in ssrf_err or "No hostname" in ssrf_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid api_base URL: {ssrf_err}",
                )

    cfg = await upsert_org_llm_config(
        org_id,
        provider=body.provider.lower(),
        model=body.model,
        api_key=body.api_key,
        api_base=body.api_base,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        timeout_seconds=body.timeout_seconds,
        routing_preference=body.routing_preference,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_llm_config_to_response(cfg),
    )


@app.delete("/llm-config", tags=["LLM Config"])
async def delete_llm_config_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Delete the authenticated org's LLM config (revert to global default)."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    deleted = await delete_org_llm_config(org_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No LLM config found.")
    return JSONResponse(content={"status": "deleted"})


# ─── Model benchmarks endpoints ──────────────────────────────────────────────


@app.get("/models/benchmarks", tags=["Models"])
async def get_models_benchmarks() -> JSONResponse:
    """Public: model catalogue with live operational benchmarks."""
    try:
        data = await build_benchmarks_response()
    except Exception:
        logger.warning("Benchmark query failed", exc_info=True)
        data = {"models": [], "updated_at": None}
    return JSONResponse(content=data)


@app.get("/models/benchmarks/org", tags=["Models"])
async def get_org_models_benchmarks(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Authenticated: model benchmarks scoped to the caller's org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")
    try:
        data = await build_benchmarks_response(org_id=org_id)
    except Exception:
        logger.warning("Org benchmark query failed for org_id=%s", org_id, exc_info=True)
        data = {"models": [], "updated_at": None}
    return JSONResponse(content=data)


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
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
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
    _mcp_updatable = (
        "name", "url", "auth_type", "auth_token",
        "auth_header_name", "timeout_seconds", "is_active",
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


# ─── A2A Delegation – Allowlist Admin ─────────────────────────────────────────


class CreateA2AAgentRequest(BaseModel):
    org_id: str
    agent_url: str = Field(..., min_length=10, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    max_cost_usdc: int = Field(default=0, description="Per-delegation cost cap in atomic USDC (0 = global default)")
    require_x402: bool = Field(default=False, description="Require x402 payment for this agent")
    jwt_forward: bool = Field(default=False, description="Forward caller JWT as Authorization header to this agent")


@app.post("/admin/a2a/agents", tags=["Admin"])
async def admin_add_a2a_agent(
    request: Request,
    body: CreateA2AAgentRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Add a trusted A2A agent to an org's allowlist."""
    pool: asyncpg.Pool = request.app.state.pool
    agent_id = str(uuid.uuid4())
    try:
        await pool.execute(
            """
            INSERT INTO a2a_allowed_agents (id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            agent_id,
            body.org_id,
            body.agent_url.rstrip("/"),
            body.label,
            body.max_cost_usdc,
            body.require_x402,
            body.jwt_forward,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This agent URL is already in the org's allowlist",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": agent_id,
            "org_id": body.org_id,
            "agent_url": body.agent_url,
            "label": body.label,
            "max_cost_usdc": body.max_cost_usdc,
            "require_x402": body.require_x402,
            "jwt_forward": body.jwt_forward,
        },
    )


@app.get("/admin/a2a/agents/{org_id}", tags=["Admin"])
async def admin_list_a2a_agents(
    request: Request,
    org_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """List all trusted A2A agents for an org."""
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward, created_at"
        " FROM a2a_allowed_agents WHERE org_id = $1 ORDER BY created_at",
        org_id,
    )
    return JSONResponse(content=[
        {
            "id": r["id"],
            "org_id": r["org_id"],
            "agent_url": r["agent_url"],
            "label": r["label"],
            "max_cost_usdc": r["max_cost_usdc"],
            "require_x402": r["require_x402"],
            "jwt_forward": r["jwt_forward"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ])


@app.delete("/admin/a2a/agents/{agent_id}", tags=["Admin"])
async def admin_delete_a2a_agent(
    request: Request,
    agent_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Remove an A2A agent from an org's allowlist."""
    pool: asyncpg.Pool = request.app.state.pool
    result = await pool.execute(
        "DELETE FROM a2a_allowed_agents WHERE id = $1",
        agent_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return JSONResponse(content={"deleted": agent_id})


# ─── A2A Delegation – Org-scoped Agent Management ────────────────────────────


class OrgCreateA2AAgentRequest(BaseModel):
    agent_url: str = Field(..., min_length=10, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    max_cost_usdc: int = Field(default=0, description="Per-delegation cost cap in atomic USDC (0 = global default)")
    require_x402: bool = Field(default=False, description="Require x402 payment for this agent")
    jwt_forward: bool = Field(default=False, description="Forward caller JWT as Authorization header to this agent")


@app.post("/a2a/agents", tags=["A2A"])
async def add_a2a_agent(
    request: Request,
    body: OrgCreateA2AAgentRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Add a trusted A2A agent to the authenticated org's allowlist."""
    org_id: str = payload.get("org_id", payload["sub"])
    pool: asyncpg.Pool = request.app.state.pool
    agent_id = str(uuid.uuid4())
    try:
        await pool.execute(
            """
            INSERT INTO a2a_allowed_agents (id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            agent_id,
            org_id,
            body.agent_url.rstrip("/"),
            body.label,
            body.max_cost_usdc,
            body.require_x402,
            body.jwt_forward,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This agent URL is already in your allowlist",
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": agent_id,
            "org_id": org_id,
            "agent_url": body.agent_url,
            "label": body.label,
            "max_cost_usdc": body.max_cost_usdc,
            "require_x402": body.require_x402,
            "jwt_forward": body.jwt_forward,
        },
    )


@app.get("/a2a/agents", tags=["A2A"])
async def list_a2a_agents(
    request: Request,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List all trusted A2A agents for the authenticated org."""
    org_id: str = payload.get("org_id", payload["sub"])
    pool: asyncpg.Pool = request.app.state.pool
    rows = await pool.fetch(
        "SELECT id, org_id, agent_url, label, max_cost_usdc, require_x402, jwt_forward, created_at"
        " FROM a2a_allowed_agents WHERE org_id = $1 ORDER BY created_at",
        org_id,
    )
    return JSONResponse(content=[
        {
            "id": r["id"],
            "agent_url": r["agent_url"],
            "label": r["label"],
            "max_cost_usdc": r["max_cost_usdc"],
            "require_x402": r["require_x402"],
            "jwt_forward": r["jwt_forward"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ])


@app.delete("/a2a/agents/{agent_id}", tags=["A2A"])
async def delete_a2a_agent(
    request: Request,
    agent_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Remove an A2A agent from the authenticated org's allowlist."""
    org_id: str = payload.get("org_id", payload["sub"])
    pool: asyncpg.Pool = request.app.state.pool
    result = await pool.execute(
        "DELETE FROM a2a_allowed_agents WHERE id = $1 AND org_id = $2",
        agent_id,
        org_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return JSONResponse(content={"deleted": agent_id})


@app.get("/a2a/delegations", tags=["A2A"])
async def list_delegation_events(
    request: Request,
    limit: int = 50,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List delegation events for the authenticated org (newest first)."""
    from billing import get_delegation_events

    org_id: str = payload.get("org_id", payload["sub"])
    events = await get_delegation_events(org_id, limit=min(limit, 200))
    return JSONResponse(content=[
        {
            "id": e["id"],
            "run_id": e["run_id"],
            "agent_url": e["agent_url"],
            "agent_name": e["agent_name"],
            "task_status": e["task_status"],
            "cost_usdc": e["cost_usdc"],
            "billing_method": e["billing_method"],
            "settlement_tx": e["settlement_tx"],
            "error": e["error"],
            "created_at": e["created_at"].isoformat() if e["created_at"] else None,
        }
        for e in events
    ])


# ─── MCP Marketplace – JSON-RPC Handler ─────────────────────────────────────


async def _execute_marketplace_tool(tool_row: dict[str, Any], arguments: dict[str, Any]) -> Any:
    """Execute a published marketplace tool via its webhook.

    ``tool_row`` is the raw DB row returned by ``get_marketplace_tool_by_name()``.
    Follows the same SSRF-safe webhook pattern as ``_build_langchain_tool``.
    """
    import aiohttp  # noqa: PLC0415

    from org_tools import _decrypt_header  # noqa: PLC0415
    from tools.definitions.http_fetch import async_validate_url  # noqa: PLC0415

    url = tool_row["webhook_url"]
    method = tool_row.get("webhook_method", "POST")
    timeout_sec = tool_row.get("timeout_seconds", 10)

    url_err = await async_validate_url(url)
    if url_err:
        return {"error": f"Webhook URL blocked: {url_err}"}

    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth_name = tool_row.get("auth_header_name")
    auth_enc = tool_row.get("auth_header_enc")
    if auth_name and auth_enc:
        try:
            headers[auth_name] = _decrypt_header(auth_enc)
        except Exception:
            return {"error": "Failed to decrypt webhook auth header"}

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "GET":
                resp = await session.get(url, headers=headers, params=arguments)
            elif method == "PUT":
                resp = await session.put(url, headers=headers, json=arguments)
            else:
                resp = await session.post(url, headers=headers, json=arguments)

            body = await resp.read()
            # 512 KB response cap
            if len(body) > 512 * 1024:
                body = body[: 512 * 1024]

            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(body)
            return {"text": body.decode("utf-8", errors="replace")}
    except asyncio.TimeoutError:
        return {"error": f"Webhook timed out after {timeout_sec}s"}
    except Exception as exc:
        return {"error": f"Webhook request failed: {type(exc).__name__}"}


def _jsonrpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _jsonrpc_result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


@app.post("/mcp/v1", tags=["MCP Marketplace"])
async def mcp_jsonrpc_handler(
    request: Request,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """MCP Streamable HTTP endpoint — JSON-RPC 2.0.

    Implements the following MCP methods:
      - ``initialize`` – returns server capabilities
      - ``tools/list`` – returns available tool definitions with pricing
      - ``tools/call`` – execute a tool (subject to billing gate)
    """
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP marketplace is not enabled.",
        )

    user_id: str = payload["sub"]
    org_id: str = payload.get("org_id", "")

    # Rate limit (separate MCP bucket)
    allowed, remaining, reset_at = await _check_rate_limit(
        f"mcp:{user_id}", s.rate_limit_mcp_rpm,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="MCP rate limit exceeded.",
            headers={
                "X-RateLimit-Limit": str(s.rate_limit_mcp_rpm),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": "60",
            },
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=_jsonrpc_error(None, -32700, "Parse error"),
            status_code=200,
        )

    req_id = body.get("id")
    method = body.get("method", "")

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(content=_jsonrpc_error(req_id, -32600, "Invalid JSON-RPC version"))

    # ── initialize ────────────────────────────────────────────────────────
    if method == "initialize":
        return JSONResponse(content=_jsonrpc_result(req_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "teardrop-marketplace", "version": app.version},
        }))

    # ── tools/list ────────────────────────────────────────────────────────
    if method == "tools/list":
        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        catalog = await get_marketplace_catalog(overrides, default_cost)

        tools_list = [
            {
                "name": t.qualified_name,
                "description": t.marketplace_description,
                "inputSchema": t.input_schema,
            }
            for t in catalog
        ]

        # Include built-in tools as well
        for bt in registry.list_latest():
            tools_list.append({
                "name": bt.name,
                "description": bt.description,
                "inputSchema": bt.input_schema.model_json_schema(),
            })

        return JSONResponse(content=_jsonrpc_result(req_id, {"tools": tools_list}))

    # ── tools/call ────────────────────────────────────────────────────────
    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not tool_name:
            return JSONResponse(content=_jsonrpc_error(req_id, -32602, "Missing tool name"))

        # Determine tool cost
        overrides = await get_tool_pricing_overrides()
        pricing = await get_current_pricing()
        default_cost = pricing.tool_call_cost if pricing else 0

        # Check if it's a marketplace tool (qualified_name = "org_slug/tool_name")
        is_marketplace_tool = "/" in tool_name
        if is_marketplace_tool:
            tool_org_slug, actual_tool_name = tool_name.split("/", 1)
        else:
            tool_org_slug, actual_tool_name = "", tool_name

        # Subscription gate: marketplace tools require an active subscription.
        if is_marketplace_tool:
            if not await check_org_subscription(org_id, tool_name):
                logger.info(
                    "mcp/v1 subscription check failed org_id=%s tool=%s", org_id, tool_name
                )
                return JSONResponse(content=_jsonrpc_error(
                    req_id, -32001,
                    f"Not subscribed to marketplace tool '{tool_name}'. "
                    "Subscribe via POST /marketplace/subscriptions.",
                ))

        # Price resolution: admin override (qualified) > admin override (bare) > author price > default
        tool_cost = overrides.get(tool_name, overrides.get(actual_tool_name, default_cost))

        # ── Billing gate (credit-only for MCP calls) ──────────────────
        billing = BillingResult()
        if s.billing_enabled:
            billing = await verify_credit(org_id, tool_cost)
            if not billing.verified:
                return JSONResponse(content=_jsonrpc_error(
                    req_id, -32000,
                    f"Insufficient credit balance. Required: {tool_cost} USDC atomic units.",
                ))

        # ── Resolve and execute tool ──────────────────────────────────
        result: Any
        author_org_id: str | None = None

        if is_marketplace_tool:
            tool_row = await get_marketplace_tool_by_name(actual_tool_name, tool_org_slug)
            if tool_row is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            author_org_id = tool_row.get("org_id")
            # Refine cost with author base_price_usdc if no admin override exists
            author_price = tool_row.get("base_price_usdc", 0)
            if tool_name not in overrides and actual_tool_name not in overrides and author_price:
                tool_cost = author_price
            result = await _execute_marketplace_tool(tool_row, arguments)
        else:
            # Built-in tool execution
            tool_def = registry.get(tool_name)
            if tool_def is None:
                return JSONResponse(
                    content=_jsonrpc_error(req_id, -32601, f"Tool not found: {tool_name}"),
                )
            try:
                result = await tool_def.implementation(**arguments)
            except Exception as exc:
                logger.error("MCP tool execution error: %s", exc, exc_info=True)
                result = {"error": str(exc)}

        # ── Debit credits ─────────────────────────────────────────────
        debited = False
        if billing.verified and billing.billing_method == "credit":
            debited = await debit_credit(org_id, tool_cost, reason=f"mcp:{tool_name}")

        # ── Record author earnings (fire-and-forget) ──────────────────
        # Only record earnings when the caller was actually charged to prevent
        # phantom earnings entries when billing is disabled or debit failed.
        if author_org_id and tool_cost > 0 and debited:
            try:
                asyncio.create_task(
                    record_tool_call_earnings(
                        author_org_id=author_org_id,
                        caller_org_id=org_id,
                        tool_name=actual_tool_name,
                        total_cost_usdc=tool_cost,
                    )
                )
            except Exception:
                logger.debug("Failed to record author earnings", exc_info=True)

        # Format MCP-spec tool result
        if isinstance(result, dict) and "error" in result:
            return JSONResponse(content=_jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": True,
            }))

        return JSONResponse(content=_jsonrpc_result(req_id, {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result) if not isinstance(result, str) else result,
                }
            ],
            "isError": False,
        }))

    # Unknown method
    return JSONResponse(content=_jsonrpc_error(req_id, -32601, f"Method not found: {method}"))


# ─── MCP Marketplace – REST API ──────────────────────────────────────────────


class SetAuthorConfigRequest(BaseModel):
    settlement_wallet: str = Field(..., min_length=42, max_length=42)


@app.post("/marketplace/author-config", tags=["Marketplace"])
async def set_marketplace_author_config(
    body: SetAuthorConfigRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Configure or update the marketplace author settings for the org."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    try:
        config = await set_author_config(
            org_id=org_id,
            settlement_wallet=body.settlement_wallet,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(content={
        "org_id": config.org_id,
        "settlement_wallet": config.settlement_wallet,
        "created_at": config.created_at.isoformat(),
        "updated_at": config.updated_at.isoformat(),
    })


@app.get("/marketplace/author-config", tags=["Marketplace"])
async def get_marketplace_author_config_endpoint(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the marketplace author configuration for the authenticated org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    config = await get_author_config(org_id)
    if config is None:
        return JSONResponse(content={
            "org_id": org_id,
            "settlement_wallet": None,
            "created_at": None,
            "updated_at": None,
        })

    return JSONResponse(content={
        "org_id": config.org_id,
        "settlement_wallet": config.settlement_wallet,
        "created_at": config.created_at.isoformat(),
        "updated_at": config.updated_at.isoformat(),
    })


@app.get("/marketplace/balance", tags=["Marketplace"])
async def get_marketplace_balance(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Get the pending (unwithdrawn) earnings balance for the authenticated org."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    balance = await get_author_balance(org_id)
    return JSONResponse(content={"org_id": org_id, "balance_usdc": balance})


@app.get("/marketplace/earnings", tags=["Marketplace"])
async def get_marketplace_earnings(
    payload: dict = Depends(require_auth),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    tool_name: str | None = Query(default=None, max_length=64),
) -> JSONResponse:
    """Get paginated earnings history for the authenticated org.

    Optionally filter by ``tool_name`` to see earnings for a specific tool.
    """
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    cursor_dt: datetime | None = None
    if cursor is not None:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid cursor format — must be an ISO 8601 timestamp.",
            )

    earnings, next_cursor = await get_author_earnings_history(
        org_id, cursor=cursor_dt, limit=limit, tool_name=tool_name
    )
    return JSONResponse(content={
        "earnings": [
            {
                "id": e.id,
                "tool_name": e.tool_name,
                "caller_org_id": e.caller_org_id,
                "total_cost_usdc": e.amount_usdc,
                "author_share_usdc": e.author_share_usdc,
                "platform_share_usdc": e.platform_share_usdc,
                "status": e.status,
                "created_at": e.created_at.isoformat(),
            }
            for e in earnings
        ],
        "next_cursor": next_cursor,
    })


class WithdrawRequest(BaseModel):
    amount_usdc: int = Field(..., gt=0)


@app.post("/marketplace/withdraw", tags=["Marketplace"])
async def request_marketplace_withdrawal(
    body: WithdrawRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Request a withdrawal of earnings to the settlement wallet."""
    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    try:
        withdrawal = await request_withdrawal(org_id, body.amount_usdc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(status_code=status.HTTP_201_CREATED, content={
        "id": withdrawal.id,
        "org_id": withdrawal.org_id,
        "amount_usdc": withdrawal.amount_usdc,
        "wallet": withdrawal.wallet,
        "status": withdrawal.status,
        "created_at": withdrawal.created_at.isoformat(),
    })


@app.get("/marketplace/withdrawals", tags=["Marketplace"])
async def get_marketplace_withdrawals(
    payload: dict = Depends(require_auth),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    """Get paginated withdrawal history (all statuses) for the authenticated org."""
    from marketplace import list_org_withdrawals

    org_id: str = payload.get("org_id", "")
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No org_id in token.")

    cursor_dt: datetime | None = None
    if cursor is not None:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid cursor format — must be an ISO 8601 timestamp.",
            )

    withdrawals, next_cursor = await list_org_withdrawals(org_id, limit=limit, cursor=cursor_dt)
    return JSONResponse(content={
        "withdrawals": [
            {
                "id": w.id,
                "amount_usdc": w.amount_usdc,
                "wallet": w.wallet,
                "tx_hash": w.tx_hash,
                "status": w.status,
                "created_at": w.created_at.isoformat(),
                "settled_at": w.settled_at.isoformat() if w.settled_at else None,
            }
            for w in withdrawals
        ],
        "next_cursor": next_cursor,
    })


@app.get("/marketplace/catalog", tags=["Marketplace"])
async def get_marketplace_catalog_endpoint(request: Request) -> JSONResponse:
    """Public: browse available marketplace tools with pricing."""
    s = get_settings()
    if not s.marketplace_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Marketplace disabled.")

    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    allowed, remaining, reset_at = await _check_rate_limit(f"catalog:{client_ip}", s.rate_limit_auth_rpm)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={
                "X-RateLimit-Limit": str(s.rate_limit_auth_rpm),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": "60",
            },
        )

    overrides = await get_tool_pricing_overrides()
    pricing = await get_current_pricing()
    default_cost = pricing.tool_call_cost if pricing else 0

    catalog = await get_marketplace_catalog(overrides, default_cost)
    return JSONResponse(content={
        "tools": [
            {
                "name": t.qualified_name,
                "description": t.marketplace_description,
                "input_schema": t.input_schema,
                "cost_usdc": t.cost_usdc,
                "author": t.author_org_name,
            }
            for t in catalog
        ]
    })


# ─── Marketplace Subscriptions ────────────────────────────────────────────────


class SubscribeRequest(BaseModel):
    qualified_tool_name: str = Field(..., min_length=3, max_length=128, pattern=r"^[a-z0-9_-]+/[a-z0-9_]+$")


@app.post("/marketplace/subscriptions", tags=["Marketplace"])
async def subscribe_to_marketplace_tool(
    body: SubscribeRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Subscribe the authenticated org to a marketplace tool for /agent/run injection."""
    from marketplace import subscribe_to_tool

    org_id: str = payload.get("org_id", "")
    try:
        sub = await subscribe_to_tool(org_id, body.qualified_tool_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": sub.id,
            "org_id": sub.org_id,
            "qualified_tool_name": sub.qualified_tool_name,
            "is_active": sub.is_active,
            "subscribed_at": sub.subscribed_at.isoformat(),
        },
    )


@app.get("/marketplace/subscriptions", tags=["Marketplace"])
async def list_marketplace_subscriptions(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """List active marketplace subscriptions for the authenticated org."""
    from marketplace import get_org_subscriptions

    org_id: str = payload.get("org_id", "")
    subs = await get_org_subscriptions(org_id)
    return JSONResponse(content={
        "subscriptions": [
            {
                "id": s.id,
                "qualified_tool_name": s.qualified_tool_name,
                "subscribed_at": s.subscribed_at.isoformat(),
            }
            for s in subs
        ]
    })


@app.delete("/marketplace/subscriptions/{subscription_id}", tags=["Marketplace"])
async def unsubscribe_from_marketplace_tool(
    subscription_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Unsubscribe from a marketplace tool."""
    from marketplace import unsubscribe_from_tool

    org_id: str = payload.get("org_id", "")
    ok = await unsubscribe_from_tool(subscription_id, org_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found.")
    return JSONResponse(content={"unsubscribed": True})


# ─── Marketplace Admin endpoints ─────────────────────────────────────────────


@app.post("/admin/marketplace/process-withdrawal/{withdrawal_id}", tags=["Admin"])
async def admin_process_withdrawal(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: process a pending withdrawal (mark earnings as settled)."""
    try:
        withdrawal = await process_withdrawal(withdrawal_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(content={
        "id": withdrawal.id,
        "org_id": withdrawal.org_id,
        "amount_usdc": withdrawal.amount_usdc,
        "status": withdrawal.status,
    })


class CompleteWithdrawalRequest(BaseModel):
    tx_hash: str = Field(..., min_length=10, max_length=100)


@app.post("/admin/marketplace/complete-withdrawal/{withdrawal_id}", tags=["Admin"])
async def admin_complete_withdrawal(
    withdrawal_id: str,
    body: CompleteWithdrawalRequest,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: record the on-chain tx_hash for a processed withdrawal."""
    try:
        await complete_withdrawal(withdrawal_id, body.tx_hash)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return JSONResponse(content={"status": "completed", "tx_hash": body.tx_hash})


@app.get("/admin/marketplace/withdrawals", tags=["Admin"])
async def admin_list_withdrawals(
    org_id: str | None = Query(default=None),
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: list pending withdrawals, optionally filtered by org."""
    withdrawals = await list_pending_withdrawals(org_id)
    return JSONResponse(content={
        "withdrawals": [
            {
                "id": w.id,
                "org_id": w.org_id,
                "amount_usdc": w.amount_usdc,
                "wallet": w.wallet,
                "status": w.status,
                "created_at": w.created_at.isoformat(),
                "settled_at": w.settled_at.isoformat() if w.settled_at else None,
            }
            for w in withdrawals
        ]
    })


@app.post("/admin/marketplace/sweep", tags=["Admin"])
async def admin_marketplace_sweep(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: manually trigger a marketplace withdrawal sweep."""
    from marketplace import marketplace_sweep_once

    count = await marketplace_sweep_once()
    return JSONResponse(content={"processed": count})


@app.get("/admin/marketplace/sweep-status", tags=["Admin"])
async def admin_marketplace_sweep_status(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: return pending/failed/exhausted withdrawal state for operator review.

    Useful for diagnosing why an org's earnings have not been settled.
    """
    from marketplace import list_exhausted_withdrawals, list_pending_withdrawals

    pending = await list_pending_withdrawals()
    exhausted = await list_exhausted_withdrawals()

    def _fmt(w: object) -> dict:
        from marketplace import AuthorWithdrawal  # noqa: PLC0415
        assert isinstance(w, AuthorWithdrawal)
        return {
            "id": w.id,
            "org_id": w.org_id,
            "amount_usdc": w.amount_usdc,
            "status": w.status,
            "sweep_attempt_count": w.sweep_attempt_count,
            "last_sweep_error": w.last_sweep_error,
            "next_sweep_at": w.next_sweep_at.isoformat() if w.next_sweep_at else None,
            "created_at": w.created_at.isoformat(),
        }

    return JSONResponse(content={
        "pending": [_fmt(w) for w in pending],
        "exhausted": [_fmt(w) for w in exhausted],
    })


@app.post("/admin/marketplace/sweep-retry/{withdrawal_id}", tags=["Admin"])
async def admin_marketplace_sweep_retry(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: reset a failed or exhausted withdrawal so the next sweep retries it."""
    from marketplace import reset_withdrawal

    found = await reset_withdrawal(withdrawal_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Withdrawal not found or not in 'failed'/'exhausted' status.",
        )
    return JSONResponse(content={"status": "pending", "id": withdrawal_id})


@app.post("/admin/marketplace/reset-withdrawal/{withdrawal_id}", tags=["Admin"])
async def admin_reset_withdrawal(
    withdrawal_id: str,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: reset a failed withdrawal to pending so it can be re-processed."""
    from marketplace import reset_withdrawal

    found = await reset_withdrawal(withdrawal_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Withdrawal not found or not in 'failed' status.",
        )
    return JSONResponse(content={"status": "pending", "id": withdrawal_id})


@app.get("/admin/marketplace/settlement-balance", tags=["Admin"])
async def admin_marketplace_settlement_balance(
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """Admin: query the USDC balance of the marketplace settlement CDP wallet."""
    from agent_wallets import _get_cdp_client, _require_cdp_enabled, _chain_id_to_network

    settings = get_settings()
    _require_cdp_enabled()
    chain_id = settings.marketplace_settlement_chain_id
    network = _chain_id_to_network(chain_id)
    account_name = settings.marketplace_settlement_cdp_account

    balance_usdc = 0
    async with _get_cdp_client() as cdp:
        account = await cdp.evm.get_or_create_account(name=account_name)
        balances = await cdp.evm.list_token_balances(
            address=account.address, network=network
        )
        for tb in balances:
            symbol = getattr(tb, "symbol", "") or ""
            if symbol.upper() == "USDC":
                from decimal import Decimal

                amt = getattr(tb, "amount", None)
                if amt is not None:
                    balance_usdc = int(Decimal(str(amt)) * Decimal("1_000_000"))
                break

    return JSONResponse(content={
        "account": account_name,
        "address": account.address,
        "chain_id": chain_id,
        "balance_usdc": balance_usdc,
    })


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
