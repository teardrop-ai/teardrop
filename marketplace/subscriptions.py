# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Marketplace subscription CRUD, cache, and LangChain wrappers."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool

from marketplace.catalog import PLATFORM_SLUG, get_marketplace_tool_by_name
from marketplace.context import _get_pool
from marketplace.models import MarketplaceSubscription, MarketplaceTool
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


class PlatformToolSubscriptionError(ValueError):
    """Raised when callers attempt to subscribe to always-available platform tools."""


_SUBSCRIPTION_CACHE: dict[str, tuple[frozenset[str], float]] = {}


def _invalidate_subscription_cache(org_id: str) -> None:
    """Drop the cached subscription set for org_id."""
    _SUBSCRIPTION_CACHE.pop(org_id, None)


async def subscribe_to_tool(org_id: str, qualified_tool_name: str) -> MarketplaceSubscription:
    """Subscribe an org to a marketplace tool by qualified name."""
    if "/" not in qualified_tool_name:
        raise ValueError("Tool name must be qualified: {org_slug}/{tool_name}")

    org_slug, tool_name = qualified_tool_name.split("/", 1)
    if org_slug == PLATFORM_SLUG:
        raise PlatformToolSubscriptionError(
            f"'{qualified_tool_name}' is a built-in platform tool and is always available without subscription."
        )

    pool = _get_pool()
    tool_row = await get_marketplace_tool_by_name(tool_name, org_slug)
    if tool_row is None:
        raise ValueError(f"Marketplace tool not found: {qualified_tool_name}")

    current_hash = tool_row.get("schema_hash") or ""
    sub_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    try:
        await pool.execute(
            """
            INSERT INTO org_marketplace_subscriptions
                (id, org_id, qualified_tool_name, is_active, subscribed_at, subscribed_schema_hash)
            VALUES ($1, $2, $3, TRUE, $4, $5)
            ON CONFLICT (org_id, qualified_tool_name) DO UPDATE
                SET is_active = TRUE, subscribed_at = EXCLUDED.subscribed_at,
                    subscribed_schema_hash = EXCLUDED.subscribed_schema_hash
            RETURNING id
            """,
            sub_id,
            org_id,
            qualified_tool_name,
            now,
            current_hash or None,
        )
    except Exception:
        raise ValueError(f"Failed to subscribe to {qualified_tool_name}")

    _invalidate_subscription_cache(org_id)
    return MarketplaceSubscription(
        id=sub_id,
        org_id=org_id,
        qualified_tool_name=qualified_tool_name,
        is_active=True,
        subscribed_at=now,
        subscribed_schema_hash=current_hash or None,
    )


async def unsubscribe_from_tool(subscription_id: str, org_id: str) -> bool:
    """Soft-delete a subscription. Returns True if found and deactivated."""
    pool = _get_pool()
    result = await pool.execute(
        "UPDATE org_marketplace_subscriptions SET is_active = FALSE WHERE id = $1 AND org_id = $2 AND is_active = TRUE",
        subscription_id,
        org_id,
    )
    _invalidate_subscription_cache(org_id)
    return result.split()[-1] != "0"


async def get_org_subscriptions(org_id: str) -> list[MarketplaceSubscription]:
    """Return all active marketplace subscriptions for an org."""
    pool = _get_pool()
    rows = await pool.fetch(
        "SELECT id, org_id, qualified_tool_name, is_active, subscribed_at, subscribed_schema_hash"
        " FROM org_marketplace_subscriptions"
        " WHERE org_id = $1 AND is_active = TRUE"
        " ORDER BY subscribed_at",
        org_id,
    )
    return [MarketplaceSubscription(**dict(r)) for r in rows]


async def get_subscribed_tools_catalog(
    org_id: str,
    tool_overrides: dict[str, int] | None = None,
    default_tool_cost: int = 0,
) -> list[MarketplaceTool]:
    """Return active marketplace tools subscribed by an org."""
    if tool_overrides is None:
        tool_overrides = {}

    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT
            t.name,
            t.description,
            t.marketplace_description,
            t.input_schema,
            t.base_price_usdc,
            o.name AS org_name,
            o.slug AS org_slug
        FROM org_marketplace_subscriptions s
        JOIN orgs o
            ON o.slug = split_part(s.qualified_tool_name, '/', 1)
        JOIN org_tools t
            ON t.org_id = o.id
           AND t.name = split_part(s.qualified_tool_name, '/', 2)
        WHERE s.org_id = $1
          AND s.is_active = TRUE
          AND s.qualified_tool_name LIKE '%/%'
          AND t.publish_as_mcp = TRUE
          AND t.is_active = TRUE
        ORDER BY t.name
        """,
        org_id,
    )

    catalog: list[MarketplaceTool] = []
    for r in rows:
        raw_schema = r["input_schema"]
        if isinstance(raw_schema, str):
            raw_schema = json.loads(raw_schema)

        qualified = f"{r['org_slug']}/{r['name']}"
        author_price = r.get("base_price_usdc", 0)
        cost = tool_overrides.get(qualified, tool_overrides.get(r["name"], author_price or default_tool_cost))

        catalog.append(
            MarketplaceTool(
                name=r["name"],
                qualified_name=qualified,
                display_name=r["name"],
                description=r["description"],
                marketplace_description=r["marketplace_description"] or r["description"],
                input_schema=raw_schema,
                cost_usdc=cost,
                author_org_name=r["org_name"],
                author_org_slug=r["org_slug"],
            )
        )

    return catalog


async def check_org_subscription(org_id: str, qualified_tool_name: str) -> bool:
    """Return True when org_id holds an active subscription to qualified_tool_name."""
    now = time.monotonic()
    cached = _SUBSCRIPTION_CACHE.get(org_id)
    if cached is not None and now < cached[1]:
        return qualified_tool_name in cached[0]
    subs = await get_org_subscriptions(org_id)
    names = frozenset(s.qualified_tool_name for s in subs)
    ttl = get_settings().org_tools_cache_ttl_seconds
    _SUBSCRIPTION_CACHE[org_id] = (names, now + ttl)
    return qualified_tool_name in names


async def build_subscribed_marketplace_tools(
    org_id: str,
) -> tuple[list, dict[str, Any]]:
    """Build LangChain StructuredTool wrappers for subscribed marketplace tools."""
    subs = await get_org_subscriptions(org_id)
    if not subs:
        return [], {}

    tools_list: list[StructuredTool] = []
    tools_by_name: dict[str, Any] = {}

    for sub in subs:
        qualified = sub.qualified_tool_name
        if "/" not in qualified:
            continue

        org_slug, tool_name = qualified.split("/", 1)
        tool_row = await get_marketplace_tool_by_name(tool_name, org_slug)
        if tool_row is None:
            logger.debug("Subscribed tool %s no longer published; skipping", qualified)
            continue

        current_hash = tool_row.get("schema_hash") or ""
        sub_hash = sub.subscribed_schema_hash or ""
        if sub_hash and current_hash and sub_hash != current_hash:
            logger.error(
                "Schema drift detected for marketplace tool %s (subscribed=%s… current=%s…) — skipping build for org_id=%s",
                qualified,
                sub_hash[:8],
                current_hash[:8],
                org_id,
            )
            continue

        try:
            lc_tool = _build_marketplace_langchain_tool(tool_row, qualified)
            tools_list.append(lc_tool)
            tools_by_name[qualified] = lc_tool
        except Exception:
            logger.warning("Failed to build subscribed tool %s", qualified, exc_info=True)

    return tools_list, tools_by_name


def _build_marketplace_langchain_tool(
    tool_row: dict[str, Any],
    qualified_name: str,
) -> StructuredTool:
    """Wrap a marketplace tool row as a LangChain StructuredTool via webhook."""
    import json as _json

    from shared.webhook import WebhookCaller, WebhookCallError
    from tools.definitions.http_fetch import async_validate_url
    from tools.shared import build_pydantic_model, decrypt_header_value

    raw_schema = tool_row.get("input_schema", {})
    if isinstance(raw_schema, str):
        raw_schema = _json.loads(raw_schema)

    model_name = f"MPTool_{qualified_name.replace('/', '_')}_Input"
    args_model = build_pydantic_model(qualified_name, raw_schema, model_name=model_name)

    _url = tool_row["webhook_url"]
    _method = tool_row.get("webhook_method", "GET")
    _timeout_sec = tool_row.get("timeout_seconds", 10)
    _auth_name = tool_row.get("auth_header_name")
    _auth_enc = tool_row.get("auth_header_enc")
    _tool_id = tool_row.get("id")
    caller = WebhookCaller(
        url=_url,
        timeout_seconds=_timeout_sec,
        auth_header_name=_auth_name,
        auth_header_encrypted=_auth_enc,
        max_response_bytes=512 * 1024,
    )

    if _method != "GET":
        raise ValueError(f"Marketplace tool '{qualified_name}' has non-GET webhook_method '{_method}'")

    async def _call(**kwargs: Any) -> dict[str, Any]:
        from tools.health import is_breaker_tripped, record_failure, record_success

        if _tool_id and await is_breaker_tripped(str(_tool_id)):
            return {"error": "Tool temporarily unavailable (circuit breaker tripped)"}

        try:
            call_result = await caller.call_get(
                params=kwargs,
                decrypt_header=decrypt_header_value,
                validate_url=async_validate_url,
            )
        except WebhookCallError as exc:
            if _tool_id and exc.error_type not in {"ssrf_blocked", "decrypt_failure"}:
                await record_failure(str(_tool_id))
            return {"error": exc.message}
        except Exception as exc:
            if _tool_id:
                await record_failure(str(_tool_id))
            return {"error": f"Webhook request failed: {type(exc).__name__}"}

        if _tool_id:
            await record_success(str(_tool_id))

        try:
            if "application/json" in call_result.content_type:
                return _json.loads(call_result.body)
            return {"text": call_result.body.decode("utf-8", errors="replace")}
        except Exception as exc:
            if _tool_id:
                await record_failure(str(_tool_id))
            return {"error": f"Webhook request failed: {type(exc).__name__}"}

    return StructuredTool.from_function(
        coroutine=_call,
        name=qualified_name,
        description=tool_row.get("marketplace_description") or tool_row.get("description", ""),
        args_schema=args_model,
        metadata={
            "timeout_seconds": _timeout_sec,
            "output_schema": tool_row.get("output_schema"),
        },
    )
