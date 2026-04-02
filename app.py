"""Teardrop FastAPI application.

Endpoints
---------
GET  /                       – redirect to /docs
GET  /health                 – health check
POST /token                  – tri-mode auth (client-creds, email+secret, SIWE)
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
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent.graph import close_checkpointer, get_graph, init_checkpointer
from agent.state import AgentState, TaskStatus
from auth import create_access_token, require_auth
from config import get_settings
from tools import registry
from usage import (
    UsageEvent,
    close_usage_db,
    get_usage_by_org,
    get_usage_by_user,
    init_usage_db,
    record_usage_event,
)
from billing import (
    BillingResult,
    build_402_headers,
    build_402_response_body,
    calculate_run_cost_usdc,
    close_billing,
    get_billing_history,
    get_current_pricing,
    get_revenue_summary,
    init_billing,
    record_settlement,
    settle_payment,
    verify_payment,
)
from users import (
    close_user_db,
    create_org,
    create_user,
    get_user_by_email,
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
from scripts.generate_keys import generate_keypair

# ─── Logging ─────────────────────────────────────────────────────────────────

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.app_log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ─── FastAPI app ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for DB connections."""
    from migrations.runner import apply_pending

    # Ensure RSA keypair exists before config tries to read the key files.
    generate_keypair(Path(__file__).resolve().parent / "keys")

    pool = await asyncpg.create_pool(settings.pg_dsn)
    app.state.pool = pool
    await apply_pending(pool)
    await init_checkpointer()
    await init_user_db(pool)
    await init_usage_db(pool)
    await init_wallets_db(pool)
    await init_billing(pool)
    yield
    await close_billing()
    await close_wallets_db()
    await close_usage_db()
    await close_user_db()
    await close_checkpointer()
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
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Rate limiting (in-memory, per-IP, per-minute) ───────────────────────────

_rate_counters: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> bool:
    """Return True when within limit, False when exceeded."""
    now = time.time()
    window = 60.0
    limit = settings.rate_limit_requests_per_minute
    history = _rate_counters[client_ip]
    _rate_counters[client_ip] = [t for t in history if now - t < window]
    if len(_rate_counters[client_ip]) >= limit:
        return False
    _rate_counters[client_ip].append(now)
    return True


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
            "description": "Intelligence beyond the browser. A task-manager agent with LangGraph, AG-UI streaming, and A2UI rendering.",
            "version": app.version,
            "url": f"http://{settings.app_host}:{settings.app_port}",
            "capabilities": {
                "streaming": True,
                "a2ui": True,
                "mcp_tools": True,
                "multi_turn": True,
                "human_in_the_loop": True,
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
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
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
            extra_claims={"org_id": user.org_id, "email": user.email, "role": user.role},
        )
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.jwt_access_token_expire_minutes * 60,
            }
        )

    # ── Client-credentials flow (backward compatible) ──────────────────────
    if body.client_id and body.client_secret:
        if not settings.jwt_client_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="JWT client secret not configured. Set JWT_CLIENT_SECRET in .env.",
            )
        if (
            body.client_id != settings.jwt_client_id
            or not hmac.compare_digest(body.client_secret, settings.jwt_client_secret)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid client credentials",
            )
        access_token = create_access_token(subject=body.client_id)
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
    from siwe import SiweMessage
    import siwe as siwe_errors

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
        # Auto-register: create org, user, and wallet
        org = await create_org(f"wallet-{address[:10].lower()}")
        user = await create_user(
            email=f"{address.lower()}@wallet",
            secret=secrets.token_urlsafe(32),  # random, not user-facing
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
        },
    )
    return JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.jwt_access_token_expire_minutes * 60,
        }
    )


@app.get("/auth/siwe/nonce", tags=["Auth"])
async def siwe_nonce(request: Request) -> JSONResponse:
    """Generate a single-use nonce for SIWE authentication."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
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
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
        )

    user_id: str = payload["sub"]
    org_id: str = payload.get("org_id", "")
    run_id = str(uuid.uuid4())
    scoped_thread_id = f"{user_id}:{body.thread_id}"
    logger.info(
        "agent_run start run_id=%s thread_id=%s user=%s",
        run_id, scoped_thread_id, user_id,
    )

    # ── x402 billing gate ─────────────────────────────────────────────────
    billing = BillingResult()
    if settings.billing_enabled and payload.get("auth_method") == "siwe":
        payment_header = (
            request.headers.get("payment-signature")
            or request.headers.get("x-payment")
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

    async def _stream() -> AsyncIterator[dict[str, str]]:
        start_time = time.monotonic()
        yield _sse_event(_EV_RUN_STARTED, {"run_id": run_id, "thread_id": body.thread_id})

        graph = get_graph()
        initial_state = AgentState(
            messages=[HumanMessage(content=body.message)],
            metadata={
                **body.context,
                "thread_id": scoped_thread_id,
                "run_id": run_id,
                "user_id": user_id,
                "org_id": org_id,
                "_usage": {"tokens_in": 0, "tokens_out": 0, "tool_calls": 0, "tool_names": []},
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
            yield _sse_event(
                _EV_ERROR,
                {"run_id": run_id, "error": f"Agent error: {exc}"},
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

        # ── x402 settlement (after usage recorded) ────────────────────────
        if billing.verified:
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
                await record_settlement(
                    usage_event.id, 0, "", "failed",
                )
                logger.warning(
                    "Settlement failed run_id=%s: %s",
                    run_id, billing_settled.error,
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
    from siwe import SiweMessage
    import siwe as siwe_errors

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
    return JSONResponse(
        content={
            "billing_enabled": True,
            "pricing": pricing.model_dump(),
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
        content=[
            {**row, "created_at": row["created_at"].isoformat()}
            for row in history
        ]
    )


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
