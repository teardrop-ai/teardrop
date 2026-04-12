# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org custom tool registry — CRUD, caching, encryption, and webhook execution.

Allows organisations to register webhook-backed tools that are injected
into the agent at run time alongside the global tool registry.  Custom
tools are never exposed in the public A2A agent card or MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp
import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from cache import get_redis
from config import get_settings
from tools.definitions.http_fetch import validate_url

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_MAX_RESPONSE_BYTES = 50 * 1024  # 50 KB webhook response cap

# ─── Models ───────────────────────────────────────────────────────────────────


class OrgTool(BaseModel):
    """Public representation of a custom tool (never contains raw auth values)."""

    id: str
    org_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    webhook_url: str
    webhook_method: str
    has_auth: bool
    timeout_seconds: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_org_tools_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference.  Called during app lifespan startup."""
    global _pool
    _pool = pool
    logger.info("Org tools DB ready")


async def close_org_tools_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Org tools DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Org tools DB not initialised — call init_org_tools_db() first")
    return _pool


# ─── Encryption ───────────────────────────────────────────────────────────────

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return a Fernet cipher, lazily initialised from config."""
    global _fernet
    if _fernet is not None:
        return _fernet
    settings = get_settings()
    key = settings.org_tool_encryption_key
    if not key:
        raise RuntimeError(
            "ORG_TOOL_ENCRYPTION_KEY is not set — generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    _fernet = Fernet(key.encode())
    return _fernet


def _encrypt_header(value: str) -> str:
    """Encrypt a webhook auth header value.  Returns base64 Fernet token."""
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt_header(encrypted: str) -> str:
    """Decrypt a webhook auth header value."""
    return _get_fernet().decrypt(encrypted.encode()).decode()


# ─── Audit logging ────────────────────────────────────────────────────────────


async def _record_event(
    org_id: str,
    tool_id: str,
    tool_name: str,
    event_type: str,
    actor_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Insert an immutable audit event.  Best-effort — errors logged, never raised."""
    try:
        pool = _get_pool()
        await pool.execute(
            "INSERT INTO org_tool_events"
            " (id, org_id, tool_id, tool_name, event_type, actor_id, detail)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)",
            str(uuid.uuid4()),
            org_id,
            tool_id,
            tool_name,
            event_type,
            actor_id,
            json.dumps(detail or {}),
        )
    except Exception:
        logger.warning("Failed to record org tool event", exc_info=True)


# ─── CRUD ─────────────────────────────────────────────────────────────────────


async def create_org_tool(
    org_id: str,
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    webhook_url: str,
    webhook_method: str,
    auth_header_name: str | None,
    auth_header_value: str | None,
    timeout_seconds: int,
    actor_id: str,
) -> OrgTool:
    """Insert a new custom tool.  Raises on duplicate name or quota exceeded."""
    pool = _get_pool()
    settings = get_settings()

    # Quota check
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM org_tools WHERE org_id = $1 AND is_active = TRUE",
        org_id,
    )
    if count >= settings.max_org_tools:
        raise ValueError(f"Organisation tool limit reached ({settings.max_org_tools})")

    tool_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    auth_enc: str | None = None
    if auth_header_value:
        auth_enc = _encrypt_header(auth_header_value)

    try:
        await pool.execute(
            "INSERT INTO org_tools"
            " (id, org_id, name, description, input_schema,"
            "  webhook_url, webhook_method,"
            "  auth_header_name, auth_header_enc,"
            "  timeout_seconds, is_active, created_at, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, TRUE, $11, $11)",
            tool_id,
            org_id,
            name,
            description,
            json.dumps(input_schema),
            webhook_url,
            webhook_method,
            auth_header_name,
            auth_enc,
            timeout_seconds,
            now,
        )
    except asyncpg.UniqueViolationError:
        raise ValueError(f"Tool '{name}' already exists for this organisation")

    await _record_event(org_id, tool_id, name, "created", actor_id)
    await invalidate_org_tools_cache(org_id)

    return OrgTool(
        id=tool_id,
        org_id=org_id,
        name=name,
        description=description,
        input_schema=input_schema,
        webhook_url=webhook_url,
        webhook_method=webhook_method,
        has_auth=auth_header_name is not None,
        timeout_seconds=timeout_seconds,
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def _row_to_org_tool(row: asyncpg.Record) -> OrgTool:
    """Map a DB row to an OrgTool model."""
    schema_raw = row["input_schema"]
    if isinstance(schema_raw, str):
        schema_raw = json.loads(schema_raw)
    return OrgTool(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        description=row["description"],
        input_schema=schema_raw,
        webhook_url=row["webhook_url"],
        webhook_method=row["webhook_method"],
        has_auth=row["auth_header_name"] is not None,
        timeout_seconds=row["timeout_seconds"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_org_tool(tool_id: str, org_id: str) -> OrgTool | None:
    """Return a single tool scoped to the org, or None."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM org_tools WHERE id = $1 AND org_id = $2",
        tool_id,
        org_id,
    )
    if row is None:
        return None
    return _row_to_org_tool(row)


async def list_org_tools(org_id: str, *, active_only: bool = True) -> list[OrgTool]:
    """List all custom tools for an org."""
    pool = _get_pool()
    query = "SELECT * FROM org_tools WHERE org_id = $1"
    if active_only:
        query += " AND is_active = TRUE"
    query += " ORDER BY name"
    rows = await pool.fetch(query, org_id)
    return [_row_to_org_tool(r) for r in rows]


async def update_org_tool(
    tool_id: str,
    org_id: str,
    *,
    actor_id: str,
    description: str | None = None,
    webhook_url: str | None = None,
    webhook_method: str | None = None,
    auth_header_name: str | None = ...,  # type: ignore[assignment]
    auth_header_value: str | None = ...,  # type: ignore[assignment]
    timeout_seconds: int | None = None,
    is_active: bool | None = None,
) -> OrgTool | None:
    """Partial-update a tool.  Returns updated OrgTool or None if not found."""
    pool = _get_pool()

    # Fetch current row to verify ownership and build update SET clause.
    row = await pool.fetchrow(
        "SELECT * FROM org_tools WHERE id = $1 AND org_id = $2",
        tool_id,
        org_id,
    )
    if row is None:
        return None

    sets: list[str] = []
    params: list[Any] = []
    idx = 1

    def _add(col: str, val: Any) -> None:
        nonlocal idx
        sets.append(f"{col} = ${idx}")
        params.append(val)
        idx += 1

    if description is not None:
        _add("description", description)
    if webhook_url is not None:
        _add("webhook_url", webhook_url)
    if webhook_method is not None:
        _add("webhook_method", webhook_method)
    if timeout_seconds is not None:
        _add("timeout_seconds", timeout_seconds)
    if is_active is not None:
        _add("is_active", is_active)

    # Handle auth header updates (sentinel ... means "not provided")
    if auth_header_name is not ...:
        _add("auth_header_name", auth_header_name)
    if auth_header_value is not ...:
        if auth_header_value is not None:
            _add("auth_header_enc", _encrypt_header(auth_header_value))
        else:
            _add("auth_header_enc", None)

    if not sets:
        return _row_to_org_tool(row)

    _add("updated_at", datetime.now(timezone.utc))
    params.append(tool_id)
    params.append(org_id)

    query = (
        f"UPDATE org_tools SET {', '.join(sets)}"
        f" WHERE id = ${idx} AND org_id = ${idx + 1}"
        " RETURNING *"
    )
    updated = await pool.fetchrow(query, *params)

    await _record_event(org_id, tool_id, row["name"], "updated", actor_id)
    await invalidate_org_tools_cache(org_id)

    return _row_to_org_tool(updated) if updated else None


async def delete_org_tool(tool_id: str, org_id: str, *, actor_id: str) -> bool:
    """Soft-delete a tool (set is_active=False).  Returns True if found."""
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE org_tools SET is_active = FALSE, updated_at = NOW()"
        " WHERE id = $1 AND org_id = $2 AND is_active = TRUE",
        tool_id,
        org_id,
    )
    deleted = result.split()[-1] != "0"  # "UPDATE N"
    if deleted:
        # Fetch name for audit
        row = await pool.fetchrow("SELECT name FROM org_tools WHERE id = $1", tool_id)
        name = row["name"] if row else tool_id
        await _record_event(org_id, tool_id, name, "deleted", actor_id)
        await invalidate_org_tools_cache(org_id)
    return deleted


# ─── Per-org TTL cache ────────────────────────────────────────────────────────

_org_tools_cache: dict[str, tuple[list[OrgTool], float]] = {}  # {org_id: (tools, expiry)}
_org_tools_lock: asyncio.Lock | None = None


def _get_cache_lock() -> asyncio.Lock:
    global _org_tools_lock
    if _org_tools_lock is None:
        _org_tools_lock = asyncio.Lock()
    return _org_tools_lock


async def get_org_tools_cached(org_id: str) -> list[OrgTool]:
    """Return active org tools with a TTL cache (Redis → in-process fallback)."""
    settings = get_settings()
    redis = get_redis()

    # ── Redis path ────────────────────────────────────────────────────────────
    if redis is not None:
        cache_key = f"teardrop:org_tools:{org_id}"
        try:
            cached_json = await redis.get(cache_key)
            if cached_json is not None:
                items = json.loads(cached_json)
                return [OrgTool(**item) for item in items]
        except Exception:
            logger.warning("Redis org_tools cache read failed; falling back", exc_info=True)

    # ── In-process TTL cache ──────────────────────────────────────────────────
    now = time.monotonic()
    cached = _org_tools_cache.get(org_id)
    if cached is not None:
        tools, expiry = cached
        if now < expiry:
            return tools

    async with _get_cache_lock():
        # Double-check after acquiring lock
        cached = _org_tools_cache.get(org_id)
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]

        tools = await list_org_tools(org_id, active_only=True)
        ttl = settings.org_tools_cache_ttl_seconds
        _org_tools_cache[org_id] = (tools, time.monotonic() + ttl)

        # Write-through to Redis
        if (r := get_redis()) is not None:
            try:
                data = json.dumps([t.model_dump(mode="json") for t in tools])
                await r.setex(f"teardrop:org_tools:{org_id}", ttl, data)
            except Exception:
                logger.warning("Redis org_tools cache write failed (non-fatal)", exc_info=True)

        return tools


async def invalidate_org_tools_cache(org_id: str) -> None:
    """Clear the cache for a specific org.  Called after any mutation."""
    _org_tools_cache.pop(org_id, None)
    redis = get_redis()
    if redis is not None:
        try:
            await redis.delete(f"teardrop:org_tools:{org_id}")
        except Exception:
            logger.warning("Redis org_tools cache invalidation failed (non-fatal)", exc_info=True)


# ─── Dynamic Pydantic model from JSON Schema ─────────────────────────────────

_JSON_SCHEMA_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _build_pydantic_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Create a Pydantic model from a JSON Schema 'properties' dict.

    Supports basic types (string, integer, number, boolean).  Unknown types
    default to ``str``.  Required fields come from the schema's ``required``
    list; everything else is ``Optional`` with a default of ``None``.
    """
    properties = schema.get("properties", {})
    required_set = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    for field_name, field_def in properties.items():
        json_type = field_def.get("type", "string")
        py_type = _JSON_SCHEMA_TYPE_MAP.get(json_type, str)
        description = field_def.get("description", "")

        if field_name in required_set:
            fields[field_name] = (py_type, Field(..., description=description))
        else:
            fields[field_name] = (py_type | None, Field(default=None, description=description))

    model_name = f"OrgTool_{name}_Input"
    return create_model(model_name, **fields)


# ─── Webhook execution & LangChain tool building ─────────────────────────────


def _build_langchain_tool(
    tool: OrgTool,
    auth_header_name: str | None,
    auth_header_enc: str | None,
) -> StructuredTool:
    """Convert a stored OrgTool into a LangChain StructuredTool.

    The returned tool calls the webhook URL with SSRF validation, timeout,
    and response truncation.
    """
    args_model = _build_pydantic_model(tool.name, tool.input_schema)

    # Capture values in closure — avoid mutable state.
    _url = tool.webhook_url
    _method = tool.webhook_method
    _timeout = tool.timeout_seconds
    _header_name = auth_header_name
    _header_enc = auth_header_enc

    async def _call_webhook(**kwargs: Any) -> dict[str, Any]:
        # SSRF re-check at call time (DNS rebinding defense)
        url_error = validate_url(_url)
        if url_error is not None:
            return {"error": f"Webhook URL blocked: {url_error}"}

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if _header_name and _header_enc:
            try:
                headers[_header_name] = _decrypt_header(_header_enc)
            except (InvalidToken, Exception):
                return {"error": "Failed to decrypt webhook auth header"}

        timeout = aiohttp.ClientTimeout(total=_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if _method == "GET":
                    resp = await session.get(_url, headers=headers, params=kwargs)
                elif _method == "PUT":
                    resp = await session.put(_url, headers=headers, json=kwargs)
                else:
                    resp = await session.post(_url, headers=headers, json=kwargs)

                body = await resp.read()
                if len(body) > _MAX_RESPONSE_BYTES:
                    body = body[:_MAX_RESPONSE_BYTES]

                content_type = resp.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    return {
                        "error": f"Webhook returned non-JSON Content-Type: {content_type}",
                        "status": resp.status,
                    }

                return json.loads(body)

        except asyncio.TimeoutError:
            return {"error": f"Webhook timed out after {_timeout}s"}
        except aiohttp.ClientError as exc:
            return {"error": f"Webhook request failed: {type(exc).__name__}"}
        except json.JSONDecodeError:
            return {"error": "Webhook returned invalid JSON"}

    return StructuredTool.from_function(
        coroutine=_call_webhook,
        name=tool.name,
        description=tool.description,
        args_schema=args_model,
    )


async def build_org_langchain_tools(
    org_id: str,
) -> tuple[list[StructuredTool], dict[str, StructuredTool]]:
    """Build LangChain tools for all active tools belonging to an org.

    Returns ``(tools_list, tools_by_name_dict)``.
    Tools whose names collide with a global registry tool are skipped.
    """
    from tools import registry as global_registry

    org_tools = await get_org_tools_cached(org_id)
    if not org_tools:
        return [], {}

    # We need auth data to build the tools — fetch raw rows.
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, auth_header_name, auth_header_enc FROM org_tools"
        " WHERE org_id = $1 AND is_active = TRUE",
        org_id,
    )
    auth_lookup = {
        r["id"]: (r["auth_header_name"], r["auth_header_enc"]) for r in rows
    }

    tools_list: list[StructuredTool] = []
    tools_by_name: dict[str, StructuredTool] = {}

    for ot in org_tools:
        # Skip if collides with a global tool
        if global_registry.get(ot.name) is not None:
            logger.warning(
                "Org tool '%s' (org=%s) skipped — collides with global tool",
                ot.name,
                org_id,
            )
            continue

        auth_name, auth_enc = auth_lookup.get(ot.id, (None, None))
        try:
            lc_tool = _build_langchain_tool(ot, auth_name, auth_enc)
            tools_list.append(lc_tool)
            tools_by_name[ot.name] = lc_tool
        except Exception:
            logger.warning("Failed to build org tool '%s'", ot.name, exc_info=True)

    return tools_list, tools_by_name
