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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from agent.cache_prewarm import prewarm_org_prefix
from agent.graph import close_checkpointer, get_graph, init_checkpointer
from billing import (
    cleanup_expired_payment_nonces,
    close_billing,
    init_billing,
    process_pending_settlements,
)
from marketplace import (
    close_marketplace_db,
    init_marketplace_db,
)
from mcp_client import (
    close_mcp_client_db,
    init_mcp_client_db,
)
from org_tools import (
    close_org_tools_db,
    init_org_tools_db,
)
from scripts.generate_keys import generate_keypair

# Shared dependencies / SIWE / app metadata live in dedicated modules so router
# modules can import them without importing teardrop.app. Re-imported here for
# routes that remain in this module (/agent/run, /token) and for monkeypatch /
# conftest compatibility (`from teardrop.app import require_admin`).
from teardrop._meta import APP_VERSION  # noqa: E402,F401
from teardrop.agent_wallets import (
    close_agent_wallets_db,
    init_agent_wallets_db,
)
from teardrop.benchmarks import (
    close_benchmarks_db,
    init_benchmarks_db,
)
from teardrop.cache import close_redis, init_redis
from teardrop.config import Settings, get_settings
from teardrop.dependencies import (
    require_admin,  # noqa: E402,F401  re-exported for conftest (`from teardrop.main import require_admin`)
)
from teardrop.llm_config import (
    close_llm_config_db,
    init_llm_config_db,
    resolve_llm_config,
)
from teardrop.memory import (
    cleanup_expired_memories,
    close_memory_db,
    init_memory_db,
)
from teardrop.usage import (
    close_usage_db,
    init_usage_db,
)
from teardrop.users import (
    cleanup_expired_refresh_tokens,
    close_user_db,
    init_user_db,
)
from teardrop.wallets import (
    close_wallets_db,
    init_wallets_db,
)
from tools._internals._rpc_semaphore import init_chain_rate_limiter, init_chain_semaphore, init_rpc_semaphore

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


async def _run_periodic(
    name: str,
    coro_fn: Callable[[], Awaitable[Any]],
    interval: float,
    monitor_slug: str | None = None,
) -> None:
    """Run *coro_fn* every *interval* seconds with cancel + error handling.

    Cancellation is propagated; all other exceptions are logged and the loop
    continues. Per-iteration logging is the responsibility of *coro_fn*.

    When *monitor_slug* is provided and Sentry is enabled, each iteration is
    wrapped in a Sentry cron check-in so a dead loop surfaces as a missed
    monitor in Sentry. ``monitor_config`` upserts the monitor on first run.
    """
    monitor_cm = _build_cron_monitor(monitor_slug, interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        # Cancellation during coro_fn() must not be reported as a cron
        # `error` check-in. Catch it inside the monitor block so __exit__
        # records `ok`, then re-raise after.
        cancel_exc: BaseException | None = None
        try:
            if monitor_cm is not None:
                with monitor_cm():
                    try:
                        await coro_fn()
                    except asyncio.CancelledError as exc:
                        cancel_exc = exc
            else:
                await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s loop error", name)
        if cancel_exc is not None:
            raise cancel_exc


def _build_cron_monitor(slug: str | None, interval_seconds: float):
    """Return a zero-arg callable producing a Sentry cron context manager, or None.

    Returns ``None`` when no slug is given or Sentry is disabled, so callers
    can branch with a single ``is None`` check. The schedule is expressed in
    whole minutes (Sentry's smallest interval unit).
    """
    if not slug or not settings.sentry_dsn:
        return None
    try:
        from sentry_sdk.crons import monitor as sentry_monitor
    except ImportError:  # pragma: no cover - sentry_sdk pinned in requirements
        return None

    minutes = max(1, int(interval_seconds // 60) or 1)
    monitor_config = {
        "schedule": {"type": "interval", "value": minutes, "unit": "minute"},
        "checkin_margin": max(2, minutes // 4 or 2),
        "max_runtime": max(2, minutes * 2),
        "failure_issue_threshold": 2,
        "recovery_threshold": 2,
    }

    def _factory():
        return sentry_monitor(monitor_slug=slug, monitor_config=monitor_config)

    return _factory


async def _settlement_retry_iter() -> None:
    processed = await process_pending_settlements()
    if processed:
        logger.info("Settlement retry: processed %d pending settlements", processed)


async def _memory_cleanup_iter() -> None:
    deleted = await cleanup_expired_memories()
    if deleted:
        logger.info("Memory cleanup: deleted %d expired memories", deleted)


async def _refresh_token_cleanup_iter() -> None:
    deleted = await cleanup_expired_refresh_tokens()
    if deleted:
        logger.info("Refresh token cleanup: deleted %d expired tokens", deleted)


async def _x402_nonce_cleanup_iter() -> None:
    deleted = await cleanup_expired_payment_nonces()
    if deleted:
        logger.info("x402 nonce cleanup: deleted %d expired payment claims", deleted)


async def _settlement_retry_loop() -> None:
    """Periodically retry failed settlements (runs as background task)."""
    await _run_periodic(
        "Settlement retry",
        _settlement_retry_iter,
        settings.settlement_retry_interval_seconds,
        monitor_slug="settlement-retry",
    )


async def _memory_cleanup_loop() -> None:
    """Periodically delete expired memories (runs as background task)."""
    await _run_periodic(
        "Memory cleanup",
        _memory_cleanup_iter,
        settings.memory_cleanup_interval_seconds,
        monitor_slug="memory-cleanup",
    )


async def _refresh_token_cleanup_loop() -> None:
    """Periodically delete revoked+expired refresh tokens (runs as background task)."""
    await _run_periodic(
        "Refresh token cleanup",
        _refresh_token_cleanup_iter,
        settings.refresh_token_cleanup_interval_seconds,
        monitor_slug="token-cleanup",
    )


async def _x402_nonce_cleanup_loop() -> None:
    """Periodically delete expired x402 payment-nonce claims (runs as background task)."""
    await _run_periodic(
        "x402 nonce cleanup",
        _x402_nonce_cleanup_iter,
        settings.refresh_token_cleanup_interval_seconds,
        monitor_slug="x402-nonce-cleanup",
    )


async def _prewarm_cache_prefixes(pool: asyncpg.Pool) -> None:
    """Warm provider prompt caches for the most active org/model prefixes."""
    if not settings.agent_cache_prewarm_enabled:
        return

    min_runs = max(1, int(settings.agent_cache_prewarm_min_runs_24h))
    top_n = max(1, int(settings.agent_cache_prewarm_top_n))

    try:
        rows = await pool.fetch(
            """
            SELECT org_id, provider, model, COUNT(*) AS run_count
            FROM usage_events
            WHERE created_at > NOW() - INTERVAL '24 hours'
              AND provider != ''
              AND model != ''
            GROUP BY org_id, provider, model
            HAVING COUNT(*) >= $1
            ORDER BY run_count DESC
            LIMIT $2
            """,
            min_runs,
            top_n,
        )
    except Exception:
        logger.debug("cache prewarm skipped: usage_events query failed", exc_info=True)
        return
    if not rows:
        return

    warmed = 0
    cache_creation_total = 0
    for row in rows:
        org_id = str(row["org_id"])
        provider = str(row["provider"])
        model = str(row["model"])

        llm_config = None
        try:
            resolved = await resolve_llm_config(org_id)
            if resolved and resolved.get("provider") == provider and resolved.get("model") == model:
                llm_config = resolved
        except Exception:
            logger.debug("cache prewarm: resolve_llm_config failed for org %s", org_id, exc_info=True)

        usage = await prewarm_org_prefix(org_id, provider, model, llm_config=llm_config)
        warmed += 1
        cache_creation_total += int(usage.get("cache_creation_input_tokens", 0))

    logger.info(
        "Cache prewarm completed: orgs_warmed=%d total_cache_creation_tokens=%d",
        warmed,
        cache_creation_total,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for DB connections."""
    from migrations.runner import apply_pending

    # Ensure RSA keypair exists before config tries to read the key files.
    generate_keypair(Path(__file__).resolve().parent.parent / "keys")

    # Warn on insecure defaults; raise on critical misconfigurations in production.
    _validate_production_config(settings)

    async def _init_conn(conn: asyncpg.Connection) -> None:
        try:
            from pgvector.asyncpg import register_vector

            await register_vector(conn)
        except Exception:
            pass  # pgvector unavailable; memory features will be disabled

    pool = await asyncpg.create_pool(
        settings.pg_dsn,
        init=_init_conn,
        command_timeout=settings.pg_command_timeout,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    logger.info(
        "Postgres pool configured: min_size=%d max_size=%d command_timeout=%.1fs",
        settings.pg_pool_min_size,
        settings.pg_pool_max_size,
        settings.pg_command_timeout,
    )
    app.state.pool = pool
    await apply_pending(pool)
    await init_redis(settings.redis_url)
    await init_checkpointer()
    await get_graph()
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

    # Initialize global RPC semaphore to limit concurrent eth_calls across all agent runs.
    init_rpc_semaphore(settings.agent_rpc_semaphore_limit)
    init_chain_semaphore(1, settings.agent_rpc_chain_semaphore_limit)
    init_chain_semaphore(8453, settings.agent_rpc_chain_semaphore_limit)
    init_chain_rate_limiter(1, settings.agent_rpc_chain_rps_limit)
    init_chain_rate_limiter(8453, settings.agent_rpc_chain_rps_limit)

    # Launch background workers.
    bg_tasks: list[asyncio.Task] = []
    if settings.billing_enabled:
        bg_tasks.append(asyncio.create_task(_settlement_retry_loop()))
        bg_tasks.append(asyncio.create_task(_x402_nonce_cleanup_loop()))
    if settings.memory_enabled and settings.memory_ttl_days > 0:
        bg_tasks.append(asyncio.create_task(_memory_cleanup_loop()))
    if settings.marketplace_auto_sweep_enabled:
        from marketplace import _marketplace_sweep_loop

        bg_tasks.append(asyncio.create_task(_marketplace_sweep_loop()))
    if settings.agent_cache_prewarm_enabled:
        bg_tasks.append(asyncio.create_task(_prewarm_cache_prefixes(pool)))
    bg_tasks.append(asyncio.create_task(_refresh_token_cleanup_loop()))

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

    # Close shared aiohttp sessions used by tool definitions.
    try:
        from tools._internals._http_session import close_http_sessions  # noqa: PLC0415

        await close_http_sessions()
    except Exception:
        logger.warning("Failed to close shared aiohttp sessions", exc_info=True)

    # Close cached AsyncWeb3 client sessions to avoid "Unclosed client session"
    # warnings on shutdown.
    try:
        from tools._internals._web3_helpers import close_web3_clients  # noqa: PLC0415

        await close_web3_clients()
    except Exception:
        logger.warning("Failed to close web3 client sessions", exc_info=True)


from fastmcp.utilities.lifespan import combine_lifespans  # noqa: E402

from tools.mcp_server import mcp as _mcp_server  # noqa: E402

mcp_app = _mcp_server.http_app(path="/", stateless_http=True, json_response=True)

app = FastAPI(
    title="Teardrop",
    description=("The native infrastructure layer for autonomous economic agents"),
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
from teardrop.mcp_gateway import MCPGatewayMiddleware  # noqa: E402

app.add_middleware(MCPGatewayMiddleware)

# ─── MCP Streamable HTTP endpoint (Smithery / direct MCP clients) ────────────
app.mount("/tools/mcp", mcp_app)

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
