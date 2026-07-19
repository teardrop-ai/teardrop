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
GET  /agent/tools            – list tools available to the authenticated org
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

import logging
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from teardrop._background_tasks import (  # noqa: E402,F401  re-exported for compatibility
    _memory_cleanup_loop,
    _prewarm_cache_prefixes,
    _refresh_token_cleanup_loop,
    _run_periodic,
    _settlement_retry_loop,
    _x402_nonce_cleanup_loop,
)
from teardrop._lifespan import build_lifespan

# Shared dependencies / SIWE / app metadata live in dedicated modules so router
# modules can import them without importing teardrop.app. Re-imported here for
# routes that remain in this module (/agent/run, /token) and for monkeypatch /
# conftest compatibility (`from teardrop.app import require_admin`).
from teardrop._meta import APP_VERSION  # noqa: E402,F401
from teardrop.config import Settings, get_settings
from teardrop.dependencies import (
    require_admin,  # noqa: E402,F401  re-exported for conftest (`from teardrop.main import require_admin`)
)

# ─── Logging ─────────────────────────────────────────────────────────────────

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.app_log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Sentry ──────────────────────────────────────────────────────────────────
# Initialize before FastAPI() so the FastAPI/Starlette/asyncpg integrations
# can hook in. No-op when SENTRY_DSN is empty.
from shared.observability import init_sentry  # noqa: E402

init_sentry(settings)

# ─── FastAPI app ──────────────────────────────────────────────────────────────


def _validate_production_config(s: "Settings") -> None:
    """Warn on insecure defaults; fail-fast on critical misconfigurations in production."""
    is_prod = s.app_env == "production"
    prefix = "config"

    def _guard(condition: bool, msg: str) -> None:
        """Raise in production, warn elsewhere, when *condition* indicates a misconfig."""
        if not condition:
            return
        if is_prod:
            raise RuntimeError(msg)
        logger.warning("%s ⚠  %s", prefix, msg)

    # jwt_client_secret — Render auto-generates this; other deployments must set it.
    if not s.jwt_client_secret:
        if is_prod:
            raise RuntimeError(
                "JWT_CLIENT_SECRET is not set. Generate a strong random secret and set it as an environment variable."
            )
        logger.warning("%s ⚠  JWT_CLIENT_SECRET is empty — client-credentials auth is disabled", prefix)

    # CORS — wildcard is allowed in development but blocked in production.
    _guard(
        bool(s.cors_origins in ("", "*")),
        "CORS_ORIGINS is open (*). Restrict it to your frontend origin(s) in production.",
    )
    if not is_prod and s.cors_origins in ("", "*"):
        logger.info("%s ·  CORS_ORIGINS open (*) — OK for local development", prefix)

    # SIWE domain — defaults to app_host (0.0.0.0) which will fail SIWE validation.
    if not s.siwe_domain and is_prod:
        logger.warning(
            "%s ⚠  SIWE_DOMAIN is not set — SIWE wallet auth will fail domain validation",
            prefix,
        )

    # Stripe — if STRIPE_SECRET_KEY is configured, STRIPE_WEBHOOK_SECRET must also be set.
    # Without it, every inbound webhook fails signature verification and Stripe stops
    # retrying after 3 days, silently dropping payment confirmations.
    _guard(
        bool(s.stripe_secret_key and not s.stripe_webhook_secret),
        "STRIPE_SECRET_KEY is set but STRIPE_WEBHOOK_SECRET is missing. "
        "Webhook signature verification will fail and Stripe stops retrying after 3 days. "
        "Get the secret from Stripe Dashboard → Workbench → Webhooks → Click to reveal.",
    )

    # Marketplace requires billing to be enabled — without billing, tool calls are
    # free but earnings are still recorded, creating uncollectable phantom entries.
    _guard(
        bool(s.marketplace_enabled and not s.billing_enabled),
        "MARKETPLACE_ENABLED=true requires BILLING_ENABLED=true. "
        "Without billing, callers are not charged but author earnings are recorded, "
        "creating phantom ledger entries that can never be collected.",
    )

    # Marketplace requires the org tool encryption key for webhook auth decryption.
    _guard(
        bool(s.marketplace_enabled and not s.org_tool_encryption_key),
        "MARKETPLACE_ENABLED=true requires ORG_TOOL_ENCRYPTION_KEY to be set. "
        "Generate one with: "
        'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
    )

    # LLM pool key coverage — warn per missing provider, hard-fail if all are absent
    # in production.  The pool router tolerates single-tier gaps but cannot function
    # with zero configured providers.
    _provider_key_map = {
        "openrouter": s.openrouter_api_key,
        "anthropic": s.anthropic_api_key,
        "google": s.google_api_key,
        "openai": s.openai_api_key,
    }
    pool_providers = {entry["provider"] for entry in s.default_model_pool}
    missing_providers = [p for p in pool_providers if not _provider_key_map.get(p, "")]
    configured_count = len(pool_providers) - len(missing_providers)
    for mp in missing_providers:
        logger.warning(
            "%s ⚠  %s_API_KEY is unset but default_model_pool includes '%s' — that routing tier will fail at runtime",
            prefix,
            mp.upper().replace("-", "_"),
            mp,
        )
    if missing_providers and configured_count == 0 and is_prod:
        raise RuntimeError(
            "All LLM providers in default_model_pool are missing API keys in production. "
            f"Missing: {', '.join(sorted(missing_providers))}. "
            "Set at least one of ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, "
            "or OPENROUTER_API_KEY."
        )
    logger.info(
        "%s   LLM pool: %d/%d providers configured%s",
        prefix,
        configured_count,
        len(pool_providers),
        f" (missing: {', '.join(sorted(missing_providers))})" if missing_providers else "",
    )

    # CDP / Marketplace production guards ────────────────────────────────────
    # Guard 1: CDP credentials must be present when marketplace + agent wallets are both on.
    _guard(
        bool(s.marketplace_enabled and s.agent_wallet_enabled and not s.cdp_configured),
        "MARKETPLACE_ENABLED=true + AGENT_WALLET_ENABLED=true requires all three "
        "CDP credentials: CDP_API_KEY_ID, CDP_API_KEY_SECRET, CDP_WALLET_SECRET. "
        "Withdrawals will fail for every request until these are set.",
    )

    # Guard 2: Testnet network must not be used in production.
    if is_prod and s.marketplace_enabled and s.cdp_network == "base-sepolia":
        raise RuntimeError(
            "CDP_NETWORK is 'base-sepolia' (testnet) in a production deployment. Set CDP_NETWORK=base to use Base mainnet."
        )

    # Guard 3: Settlement chain ID must be Base mainnet in production.
    if is_prod and s.marketplace_enabled and s.marketplace_settlement_chain_id == 84532:
        raise RuntimeError(
            "MARKETPLACE_SETTLEMENT_CHAIN_ID is 84532 (Base Sepolia testnet) in production. "
            "Set MARKETPLACE_SETTLEMENT_CHAIN_ID=8453 for Base mainnet."
        )

    # Guard 4: Mismatched network name vs chain ID (silent config error).
    _network_chain_pairs = {"base-sepolia": 84532, "base": 8453}
    _expected_chain = _network_chain_pairs.get(s.cdp_network)
    if s.marketplace_enabled and _expected_chain is not None and s.marketplace_settlement_chain_id != _expected_chain:
        logger.warning(
            "%s ⚠  CDP_NETWORK='%s' expects chain_id=%d but "
            "MARKETPLACE_SETTLEMENT_CHAIN_ID=%d — network/chain mismatch may cause "
            "withdrawals to be sent to the wrong chain",
            prefix,
            s.cdp_network,
            _expected_chain,
            s.marketplace_settlement_chain_id,
        )

    # Guard 5: Sweep enabled without a private RPC URL — public fallback RPCs are
    # rate-limited and unreliable under load.  Warn but don't block startup.
    if s.marketplace_auto_sweep_enabled and not s.base_rpc_url:
        logger.warning(
            "%s ⚠  MARKETPLACE_AUTO_SWEEP_ENABLED=true but BASE_RPC_URL is unset. "
            "Transaction verification will use public RPC endpoints which may be "
            "rate-limited under load. Set BASE_RPC_URL to a dedicated RPC provider.",
            prefix,
        )

    # Log CDP state so operators can confirm configuration at startup.
    if s.agent_wallet_enabled or s.marketplace_enabled:
        logger.info(
            "%s   CDP: wallet_enabled=%s configured=%s network=%s settlement_account=%s settlement_chain=%d tx_timeout=%ds",
            prefix,
            s.agent_wallet_enabled,
            s.cdp_configured,
            s.cdp_network,
            s.marketplace_settlement_cdp_account,
            s.marketplace_settlement_chain_id,
            s.marketplace_tx_confirm_timeout_seconds,
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


lifespan = build_lifespan(_validate_production_config)


from fastmcp.utilities.lifespan import combine_lifespans  # noqa: E402

from tools.mcp_server import mcp as _mcp_server  # noqa: E402

mcp_app = _mcp_server.http_app(path="/", stateless_http=True, json_response=True)

app = FastAPI(
    title="Teardrop",
    description=("The open API and billing gateway for autonomous economic agents"),
    version=APP_VERSION,
    lifespan=combine_lifespans(lifespan, mcp_app.lifespan),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ─── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Payment-Signature", "X-Payment"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "X-RateLimit-Scope", "Retry-After"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Apply baseline security headers to every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
    if settings.app_env == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# ─── MCP gateway (auth / billing / x402 — wraps FastMCP ASGI app) ────────────
from teardrop.mcp_gateway import MCPGatewayMiddleware, MCPPathNormalizer  # noqa: E402

# Keep the gateway on the Streamable HTTP mount only. REST endpoints such as
# /mcp/servers/{server_id}/discover must not traverse BaseHTTPMiddleware.
app.add_middleware(MCPPathNormalizer)
app.mount("/tools/mcp", MCPGatewayMiddleware(mcp_app, mounted=True))

# ─── Domain routers (system/discovery extracted into teardrop.routers) ───────
from teardrop.routers import register_routers  # noqa: E402

register_routers(app)

# ─── Rate limiting (sliding-window, Redis-first with in-process fallback) ─────
# Implementation lives in teardrop.rate_limit so router modules share a single
# in-process ``_rate_counters`` instance. Re-imported here for monkeypatch
# compatibility (test fixtures clear ``teardrop.main._rate_counters``).
from teardrop.rate_limit import (  # noqa: E402
    _RATE_COUNTER_MAX_KEYS,  # noqa: F401
    RateLimitResult,  # noqa: F401  re-exported for monkeypatch/import compatibility
    _enforce_rate_limit,  # noqa: F401  re-exported for monkeypatch/import compatibility
    _rate_counters,  # noqa: F401  test fixtures clear teardrop.main._rate_counters
)

# ─── Agent routes (POST /agent/run, GET /agent/tools) ─────────────────────────
# The streaming agent endpoints live in teardrop.routers.agent. The public
# handlers and request/response models are re-exported here so downstream SDK
# code that imports ``from teardrop.main import ...`` keeps a stable surface.
from teardrop.routers.agent import (  # noqa: E402,F401
    AgentRunRequest,
    AgentToolItem,
    ToolPolicy,
    _normalize_exclusion_name,
    agent_run,
    list_agent_tools,
)

# ─── Auth helpers ─────────────────────────────────────────────────────────────
# require_admin and _require_org_id are imported from teardrop.dependencies at
# the top of this module.


# ─── Admin endpoints (extracted to routers.admin) ───────────────────────────


# ─── LLM Config + Model benchmarks endpoints (extracted to routers.org.llm_config) ──


# ─── Memory endpoints (extracted to routers.org.memory) ─────────────────────


# ─── A2A Delegation – Org-scoped Agent Management (extracted to routers.org.a2a) ──


# ─── MCP Marketplace + REST API (extracted to routers.marketplace) ──────────


# ─── Marketplace Admin endpoints (extracted to routers.admin) ────────────────


# ─── Entry point for `python -m teardrop.main` ───────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "teardrop.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.app_log_level,
        reload=settings.app_env == "development",
    )
