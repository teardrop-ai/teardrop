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
import sentry_sdk
from cryptography.fernet import Fernet, InvalidToken
from jsonschema import Draft7Validator
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from cache import TTLCache, get_redis
from config import get_settings
from tools.definitions.http_fetch import async_validate_url

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
    output_schema: dict[str, Any] | None = None
    webhook_url: str
    webhook_method: str
    has_auth: bool
    timeout_seconds: int
    is_active: bool
    publish_as_mcp: bool = False
    marketplace_description: str = ""
    base_price_usdc: int = 0
    schema_hash: str = ""
    last_schema_changed_at: datetime | None = None
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
    output_schema: dict[str, Any] | None = None,
    publish_as_mcp: bool = False,
    marketplace_description: str = "",
    base_price_usdc: int = 0,
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

    # Validate: publishing requires author config
    if publish_as_mcp:
        from marketplace import get_author_config

        config = await get_author_config(org_id)
        if config is None:
            raise ValueError(
                "Cannot publish tool to marketplace — register a settlement wallet first via POST /marketplace/author-config"
            )

    try:
        await pool.execute(
            "INSERT INTO org_tools"
            " (id, org_id, name, description, input_schema, output_schema,"
            "  webhook_url, webhook_method,"
            "  auth_header_name, auth_header_enc,"
            "  timeout_seconds, is_active,"
            "  publish_as_mcp, marketplace_description, base_price_usdc,"
            "  created_at, updated_at, last_schema_changed_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, TRUE, $12, $13, $14, $15, $15, $15)",
            tool_id,
            org_id,
            name,
            description,
            json.dumps(input_schema),
            json.dumps(output_schema) if output_schema is not None else None,
            webhook_url,
            webhook_method,
            auth_header_name,
            auth_enc,
            timeout_seconds,
            publish_as_mcp,
            marketplace_description,
            base_price_usdc,
            now,
        )
    except asyncpg.UniqueViolationError:
        raise ValueError(f"Tool '{name}' already exists for this organisation")

    await _record_event(org_id, tool_id, name, "created", actor_id)
    await invalidate_org_tools_cache(org_id)
    if publish_as_mcp:
        await invalidate_marketplace_cache()

    return OrgTool(
        id=tool_id,
        org_id=org_id,
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        webhook_url=webhook_url,
        webhook_method=webhook_method,
        has_auth=auth_header_name is not None,
        timeout_seconds=timeout_seconds,
        is_active=True,
        publish_as_mcp=publish_as_mcp,
        marketplace_description=marketplace_description,
        base_price_usdc=base_price_usdc,
        last_schema_changed_at=now,
        created_at=now,
        updated_at=now,
    )


def _row_to_org_tool(row: asyncpg.Record) -> OrgTool:
    """Map a DB row to an OrgTool model."""
    schema_raw = row["input_schema"]
    if isinstance(schema_raw, str):
        schema_raw = json.loads(schema_raw)
    output_schema_raw = row.get("output_schema")
    if isinstance(output_schema_raw, str):
        output_schema_raw = json.loads(output_schema_raw)
    return OrgTool(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        description=row["description"],
        input_schema=schema_raw,
        output_schema=output_schema_raw,
        webhook_url=row["webhook_url"],
        webhook_method=row["webhook_method"],
        has_auth=row["auth_header_name"] is not None,
        timeout_seconds=row["timeout_seconds"],
        is_active=row["is_active"],
        publish_as_mcp=row.get("publish_as_mcp", False),
        marketplace_description=row.get("marketplace_description", ""),
        base_price_usdc=row.get("base_price_usdc", 0),
        schema_hash=row.get("schema_hash") or "",
        last_schema_changed_at=row.get("last_schema_changed_at"),
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
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    webhook_url: str | None = None,
    webhook_method: str | None = None,
    auth_header_name: str | None = ...,  # type: ignore[assignment]
    auth_header_value: str | None = ...,  # type: ignore[assignment]
    timeout_seconds: int | None = None,
    is_active: bool | None = None,
    publish_as_mcp: bool | None = None,
    marketplace_description: str | None = None,
    base_price_usdc: int | None = None,
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
    if input_schema is not None:
        _add("input_schema", json.dumps(input_schema))
        _add("last_schema_changed_at", datetime.now(timezone.utc))
    if output_schema is not None:
        _add("output_schema", json.dumps(output_schema))
    if webhook_url is not None:
        _add("webhook_url", webhook_url)
    if webhook_method is not None:
        _add("webhook_method", webhook_method)
    if timeout_seconds is not None:
        _add("timeout_seconds", timeout_seconds)
    if is_active is not None:
        _add("is_active", is_active)
    if publish_as_mcp is not None:
        # Validate: publishing requires author config
        if publish_as_mcp:
            from marketplace import get_author_config

            config = await get_author_config(org_id)
            if config is None:
                raise ValueError("Cannot publish tool to marketplace — register a settlement wallet first")
        _add("publish_as_mcp", publish_as_mcp)
    if marketplace_description is not None:
        _add("marketplace_description", marketplace_description)
    if base_price_usdc is not None:
        _add("base_price_usdc", base_price_usdc)

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

    query = f"UPDATE org_tools SET {', '.join(sets)} WHERE id = ${idx} AND org_id = ${idx + 1} RETURNING *"
    updated = await pool.fetchrow(query, *params)

    await _record_event(org_id, tool_id, row["name"], "updated", actor_id)
    await invalidate_org_tools_cache(org_id)
    # Invalidate marketplace cache if publishing status may have changed
    if publish_as_mcp is not None or is_active is not None:
        await invalidate_marketplace_cache()

    # Clear circuit breaker state on FALSE → TRUE transition so the tool
    # starts with a clean failure window after manual re-enable.
    if is_active is True and row["is_active"] is False:
        try:
            from tool_health import clear_breaker

            await clear_breaker(tool_id)
        except Exception:  # pragma: no cover
            logger.debug("clear_breaker failed during re-enable", exc_info=True)

    return _row_to_org_tool(updated) if updated else None


async def delete_org_tool(tool_id: str, org_id: str, *, actor_id: str) -> bool:
    """Soft-delete a tool (set is_active=False).  Returns True if found."""
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE org_tools SET is_active = FALSE, updated_at = NOW() WHERE id = $1 AND org_id = $2 AND is_active = TRUE",
        tool_id,
        org_id,
    )
    deleted = result.split()[-1] != "0"  # "UPDATE N"
    if deleted:
        # Fetch name for audit
        row = await pool.fetchrow("SELECT name, publish_as_mcp FROM org_tools WHERE id = $1", tool_id)
        name = row["name"] if row else tool_id
        await _record_event(org_id, tool_id, name, "deleted", actor_id)
        await invalidate_org_tools_cache(org_id)

        # Clear breaker state so a future re-creation starts fresh.
        try:
            from tool_health import clear_breaker

            await clear_breaker(tool_id)
        except Exception:  # pragma: no cover
            logger.debug("clear_breaker failed during delete", exc_info=True)

        if row and row.get("publish_as_mcp"):
            await invalidate_marketplace_cache()
            # Deactivate all marketplace subscriptions for this tool so subscribers
            # are not silently left with a dead tool reference.
            org_row = await pool.fetchrow("SELECT slug FROM orgs WHERE id = $1", org_id)
            qualified_name: str | None = None
            if org_row:
                qualified_name = f"{org_row['slug']}/{name}"
                await pool.execute(
                    "UPDATE org_marketplace_subscriptions SET is_active = FALSE"
                    " WHERE qualified_tool_name = $1 AND is_active = TRUE",
                    qualified_name,
                )

            # Notify subscribers (fire-and-forget).
            if qualified_name is not None:
                try:
                    from marketplace import notify_subscribers_of_deactivation

                    asyncio.create_task(
                        notify_subscribers_of_deactivation(
                            qualified_name,
                            "manually removed by author",
                        )
                    )
                except Exception:  # pragma: no cover
                    logger.debug("Failed to schedule subscriber notification", exc_info=True)
    return deleted


# ─── Per-org TTL cache ────────────────────────────────────────────────────────

_org_tool_caches: dict[str, TTLCache[list[OrgTool]]] = {}


def _get_org_tool_cache(org_id: str) -> TTLCache[list[OrgTool]]:
    if org_id not in _org_tool_caches:
        _org_tool_caches[org_id] = TTLCache(
            name=f"org_tools:{org_id}",
            redis_key=f"teardrop:org_tools:{org_id}",
            ttl_seconds_fn=lambda: get_settings().org_tools_cache_ttl_seconds,
            loader=lambda: list_org_tools(org_id, active_only=True),
            serialize=lambda tools: json.dumps([t.model_dump(mode="json") for t in tools]),
            deserialize=lambda raw: [OrgTool(**item) for item in json.loads(raw)],
        )
    return _org_tool_caches[org_id]


async def get_org_tools_cached(org_id: str) -> list[OrgTool]:
    """Return active org tools with a TTL cache (Redis → in-process fallback)."""
    return await _get_org_tool_cache(org_id).get() or []


async def invalidate_org_tools_cache(org_id: str) -> None:
    """Clear the cache for a specific org.  Called after any mutation."""
    await _get_org_tool_cache(org_id).invalidate()


# ─── Marketplace tools cache ─────────────────────────────────────────────────

_marketplace_cache: tuple[list[OrgTool], float] | None = None  # (tools, expiry)
_marketplace_lock: asyncio.Lock | None = None


def _get_marketplace_lock() -> asyncio.Lock:
    global _marketplace_lock
    if _marketplace_lock is None:
        _marketplace_lock = asyncio.Lock()
    return _marketplace_lock


async def list_marketplace_tools() -> list[OrgTool]:
    """Return all published marketplace tools with a TTL cache."""
    global _marketplace_cache
    settings = get_settings()
    redis = get_redis()

    # Redis path
    if redis is not None:
        try:
            cached_json = await redis.get("teardrop:marketplace:tools")
            if cached_json is not None:
                items = json.loads(cached_json)
                return [OrgTool(**item) for item in items]
        except Exception:
            logger.warning("Redis marketplace cache read failed; falling back", exc_info=True)

    # In-process TTL cache
    now = time.monotonic()
    if _marketplace_cache is not None and now < _marketplace_cache[1]:
        return _marketplace_cache[0]

    async with _get_marketplace_lock():
        if _marketplace_cache is not None and time.monotonic() < _marketplace_cache[1]:
            return _marketplace_cache[0]

        pool = _get_pool()
        rows = await pool.fetch("SELECT * FROM org_tools WHERE publish_as_mcp = TRUE AND is_active = TRUE ORDER BY name")
        tools = [_row_to_org_tool(r) for r in rows]
        ttl = settings.org_tools_cache_ttl_seconds
        _marketplace_cache = (tools, time.monotonic() + ttl)

        if (r := get_redis()) is not None:
            try:
                data = json.dumps([t.model_dump(mode="json") for t in tools])
                await r.setex("teardrop:marketplace:tools", ttl, data)
            except Exception:
                logger.warning("Redis marketplace cache write failed (non-fatal)", exc_info=True)

        return tools


async def invalidate_marketplace_cache() -> None:
    """Clear the marketplace tools cache.  Called after publish/unpublish mutations."""
    global _marketplace_cache
    _marketplace_cache = None
    redis = get_redis()
    if redis is not None:
        try:
            await redis.delete("teardrop:marketplace:tools")
        except Exception:
            logger.warning("Redis marketplace cache invalidation failed (non-fatal)", exc_info=True)


# ─── Dynamic Pydantic model from JSON Schema ─────────────────────────────────

_JSON_SCHEMA_TYPE_MAP: dict[str, type] = {
    "object": dict,
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
}

_SAFE_SCHEMA_KEYS: set[str] = {
    "type",
    "properties",
    "required",
    "description",
    "title",
    "default",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "items",
}

_SAFE_SCHEMA_TYPES: set[str] = {"object", "string", "integer", "number", "boolean", "array"}


def normalize_webhook_response(
    raw: bytes,
    *,
    content_type: str,
    status_code: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Normalize webhook HTTP response payload into a consistent shape."""
    body_bytes = raw[:max_bytes]

    if "application/json" not in content_type:
        return {
            "success": False,
            "status_code": status_code,
            "response_body": None,
            "error": f"Webhook returned non-JSON Content-Type: {content_type or 'unset'}",
            "error_type": "non_json_response",
        }

    try:
        parsed = json.loads(body_bytes)
    except json.JSONDecodeError:
        return {
            "success": False,
            "status_code": status_code,
            "response_body": None,
            "error": "Webhook returned invalid or truncated JSON",
            "error_type": "invalid_json",
        }

    response_body = parsed if isinstance(parsed, dict) else {"value": parsed}

    if status_code >= 400:
        return {
            "success": False,
            "status_code": status_code,
            "response_body": response_body,
            "error": f"Webhook returned HTTP {status_code}",
            "error_type": "http_error",
        }

    return {
        "success": True,
        "status_code": status_code,
        "response_body": response_body,
        "error": None,
        "error_type": None,
    }


def validate_safe_schema_subset(schema: dict[str, Any]) -> list[str]:
    """Return unsupported schema keywords/types that runtime tooling cannot enforce."""
    errors: list[str] = []

    def _walk(node: Any, path: str, *, in_properties_map: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if in_properties_map:
                    # Property names are user-defined keys that each hold a schema node.
                    _walk(value, f"{path}.{key}")
                    continue
                if key not in _SAFE_SCHEMA_KEYS:
                    errors.append(f"{path}.{key}: keyword not supported")
                if key == "type":
                    if isinstance(value, str):
                        if value not in _SAFE_SCHEMA_TYPES:
                            errors.append(f"{path}.type={value}: type not supported")
                    elif isinstance(value, list):
                        bad = [t for t in value if t not in _SAFE_SCHEMA_TYPES]
                        if bad:
                            errors.append(f"{path}.type={bad}: type not supported")
                    else:
                        errors.append(f"{path}.type: invalid type declaration")
                _walk(value, f"{path}.{key}", in_properties_map=(key == "properties"))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]", in_properties_map=False)

    try:
        Draft7Validator.check_schema(schema)
    except Exception:
        # Schema syntax errors are validated at API layer.
        return ["$.schema: invalid JSON Schema"]

    _walk(schema, "$")
    return errors


def _build_pydantic_model(
    name: str,
    schema: dict[str, Any],
    model_name: str | None = None,
) -> type[BaseModel]:
    """Create a Pydantic model from a JSON Schema 'properties' dict.

    Supports basic types (string, integer, number, boolean).  Unknown types
    default to ``str``.  Required fields come from the schema's ``required``
    list; everything else is ``Optional`` with a default of ``None``.

    ``model_name`` overrides the auto-generated class name; defaults to
    ``OrgTool_{name}_Input``.
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

    cls_name = model_name if model_name is not None else f"OrgTool_{name}_Input"
    return create_model(cls_name, **fields)


# ─── Webhook execution & LangChain tool building ─────────────────────────────


def _hash_webhook_host(url: str) -> str:
    """Return a short, non-reversible host fingerprint for Sentry tagging.

    Avoids leaking full URLs (which may include path tokens) while still
    permitting cluster-level analysis of failing hosts.
    """
    import hashlib
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc or "unknown"
    except Exception:
        host = "unknown"
    return hashlib.sha256(host.encode()).hexdigest()[:12]


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
    _tool_id = tool.id
    _org_id = tool.org_id
    _tool_name = tool.name
    _url = tool.webhook_url
    _method = tool.webhook_method
    _timeout = tool.timeout_seconds
    _header_name = auth_header_name
    _header_enc = auth_header_enc
    _host_hash = _hash_webhook_host(_url)

    async def _call_webhook(**kwargs: Any) -> dict[str, Any]:
        from tool_health import is_breaker_tripped, record_success

        # Pre-execution gate: skip immediately if breaker is tripped.
        if await is_breaker_tripped(_tool_id):
            return {"error": "Tool temporarily unavailable (circuit breaker tripped)"}

        # SSRF re-check at call time (DNS rebinding defense)
        url_error = await async_validate_url(_url)
        if url_error is not None:
            return {"error": f"Webhook URL blocked: {url_error}"}

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if _header_name and _header_enc:
            try:
                headers[_header_name] = _decrypt_header(_header_enc)
            except (InvalidToken, Exception):
                await _on_webhook_failure(_tool_id, _org_id, _tool_name, _host_hash, "decrypt_failure")
                return {"error": "Failed to decrypt webhook auth header"}

        timeout = aiohttp.ClientTimeout(total=_timeout)
        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if _method == "GET":
                    resp = await session.get(_url, headers=headers, params=kwargs)
                elif _method == "PUT":
                    resp = await session.put(_url, headers=headers, json=kwargs)
                else:
                    resp = await session.post(_url, headers=headers, json=kwargs)

                body = await resp.read()
                content_type = resp.headers.get("Content-Type", "")
                normalized = normalize_webhook_response(
                    body,
                    content_type=content_type,
                    status_code=resp.status,
                    max_bytes=_MAX_RESPONSE_BYTES,
                )

                if not normalized["success"]:
                    await _on_webhook_failure(
                        _tool_id,
                        _org_id,
                        _tool_name,
                        _host_hash,
                        normalized["error_type"] or "upstream_error",
                        status_code=resp.status,
                    )
                    return {"error": normalized["error"], "status": resp.status}

                # Success path.
                latency_ms = int((time.monotonic() - started) * 1000)
                await record_success(_tool_id)
                await _record_event(
                    _org_id,
                    _tool_id,
                    _tool_name,
                    "executed",
                    actor_id="agent",
                    detail={"latency_ms": latency_ms, "status": resp.status},
                )
                return normalized["response_body"]

        except asyncio.TimeoutError:
            await _on_webhook_failure(_tool_id, _org_id, _tool_name, _host_hash, "timeout")
            return {"error": f"Webhook timed out after {_timeout}s"}
        except aiohttp.ClientError as exc:
            await _on_webhook_failure(_tool_id, _org_id, _tool_name, _host_hash, type(exc).__name__)
            return {"error": f"Webhook request failed: {type(exc).__name__}"}

    return StructuredTool.from_function(
        coroutine=_call_webhook,
        name=tool.name,
        description=tool.description,
        args_schema=args_model,
        metadata={
            "timeout_seconds": tool.timeout_seconds,
            "output_schema": tool.output_schema,
        },
    )


async def _on_webhook_failure(
    tool_id: str,
    org_id: str,
    tool_name: str,
    host_hash: str,
    error_type: str,
    *,
    status_code: int | None = None,
) -> None:
    """Centralised failure side-effects: audit, breaker, sentry, deactivation."""
    from tool_health import record_failure

    detail: dict[str, Any] = {"error_type": error_type, "host_hash": host_hash}
    if status_code is not None:
        detail["status"] = status_code
    await _record_event(
        org_id,
        tool_id,
        tool_name,
        "failed",
        actor_id="agent",
        detail=detail,
    )

    tripped = False
    try:
        tripped = await record_failure(tool_id)
    except Exception:  # pragma: no cover
        logger.warning("tool_health.record_failure raised", exc_info=True)

    # Sentry: capture only on tripped transitions to avoid quota burn from
    # flapping tools.  Per-failure breadcrumbs are still emitted via logger.
    if tripped:
        try:
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("tool_id", str(tool_id))
                scope.set_tag("org_id", str(org_id))
                scope.set_tag("error_type", error_type)
                scope.set_tag("webhook_host", host_hash)
                scope.set_tag("circuit_breaker", "tripped")
                sentry_sdk.capture_message(
                    f"Webhook circuit breaker tripped: tool_id={tool_id}",
                    level="warning",
                )
        except Exception:  # pragma: no cover
            logger.debug("sentry capture failed in _on_webhook_failure", exc_info=True)

        try:
            from marketplace import auto_deactivate_tool_for_health

            await auto_deactivate_tool_for_health(tool_id)
        except Exception:  # pragma: no cover
            logger.warning("auto_deactivate_tool_for_health failed tool_id=%s", tool_id, exc_info=True)


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
        "SELECT id, auth_header_name, auth_header_enc FROM org_tools WHERE org_id = $1 AND is_active = TRUE",
        org_id,
    )
    auth_lookup = {r["id"]: (r["auth_header_name"], r["auth_header_enc"]) for r in rows}

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
