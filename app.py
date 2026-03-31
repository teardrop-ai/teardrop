"""Teardrop FastAPI application.

Endpoints
---------
GET  /                       – redirect to /docs
GET  /health                 – health check
POST /agent/run              – AG-UI streaming endpoint (SSE)
GET  /.well-known/agent-card.json – A2A agent card for discoverability
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

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
    """Startup / shutdown lifecycle for the checkpointer."""
    await init_checkpointer()
    yield
    await close_checkpointer()


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
async def health_check() -> JSONResponse:
    """Liveness probe – returns service status and version."""
    return JSONResponse(
        content={
            "status": "ok",
            "service": "teardrop",
            "version": app.version,
            "environment": settings.app_env,
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
    client_id: str
    client_secret: str


@app.post("/token", tags=["Auth"])
async def token(body: TokenRequest, request: Request) -> JSONResponse:
    """Client-credentials token endpoint. Returns a signed RS256 JWT."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
        )
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


@app.post("/agent/run", tags=["Agent"], dependencies=[Depends(require_auth)])
async def agent_run(body: AgentRunRequest, request: Request) -> EventSourceResponse:
    """AG-UI streaming endpoint.

    Accepts a user message and streams AG-UI-compatible Server-Sent Events
    until the agent completes or errors.  Supports multi-turn via thread_id.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
        )

    run_id = str(uuid.uuid4())
    logger.info("agent_run start run_id=%s thread_id=%s", run_id, body.thread_id)

    async def _stream() -> AsyncIterator[dict[str, str]]:
        yield _sse_event(_EV_RUN_STARTED, {"run_id": run_id, "thread_id": body.thread_id})

        graph = get_graph()
        initial_state = AgentState(
            messages=[HumanMessage(content=body.message)],
            metadata={**body.context, "thread_id": body.thread_id, "run_id": run_id},
        )
        config = {"configurable": {"thread_id": body.thread_id}}

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

        yield _sse_event(_EV_RUN_FINISHED, {"run_id": run_id})
        yield _sse_event(_EV_DONE, {"run_id": run_id})

    return EventSourceResponse(_stream())


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
