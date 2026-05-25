# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Billing pricing, caches, and usage-cost calculation."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from billing.context import _get_pool, _has_pool
from billing.models import PricingRule
from teardrop.cache import TTLCache, get_redis
from teardrop.config import get_settings

logger = logging.getLogger(__name__)


async def get_current_pricing() -> PricingRule | None:
    """Return the currently effective pricing rule (direct DB query, no cache)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, name, run_price_usdc, tokens_in_cost_per_1k,
               tokens_out_cost_per_1k, tool_call_cost, effective_from, created_at
        FROM pricing_rules
        WHERE effective_from <= NOW()
        ORDER BY effective_from DESC
        LIMIT 1
        """
    )
    if row is None:
        return None
    return PricingRule(**dict(row))


async def get_current_pricing_for_model(provider: str, model: str, *, is_byok: bool = False) -> PricingRule | None:
    """Return the most specific pricing rule for a provider/model (direct DB query)."""
    pool = _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, name, run_price_usdc, tokens_in_cost_per_1k,
               tokens_out_cost_per_1k, tool_call_cost, effective_from, created_at
        FROM pricing_rules
        WHERE effective_from <= NOW()
          AND is_byok = $3
          AND (
            (provider = $1 AND model = $2)
            OR (provider = $1 AND model = '')
            OR (provider = '' AND model = '')
          )
        ORDER BY
          CASE
            WHEN provider = $1 AND model = $2 THEN 0
            WHEN provider = $1 AND model = '' THEN 1
            ELSE 2
          END,
          effective_from DESC
        LIMIT 1
        """,
        provider,
        model,
        is_byok,
    )
    if row is None:
        return None
    return PricingRule(**dict(row))


# Cache key format: "{provider}:{model}:{is_byok}" to keep BYOK and standard
# pricing separate in the same dict.
_model_pricing_cache: dict[str, tuple[PricingRule | None, float]] = {}
_model_pricing_lock: asyncio.Lock | None = None


async def get_live_pricing_for_model(provider: str, model: str, *, is_byok: bool = False) -> PricingRule | None:
    """Return model-specific pricing using a TTL cache."""
    global _model_pricing_lock

    if not provider and not model:
        return await get_live_pricing()

    settings = get_settings()
    cache_key = f"{provider}:{model}:{is_byok}"
    redis = get_redis()

    # Redis path
    if redis is not None:
        try:
            rkey = f"teardrop:pricing:{provider}:{model}:{'byok' if is_byok else 'std'}"
            cached_json = await redis.get(rkey)
            if cached_json is not None:
                data = json.loads(cached_json)
                if data is None:
                    return None
                return PricingRule(**data)
        except Exception as exc:
            logger.warning("Redis model pricing cache read failed: %s", exc)

    # In-process fast path
    entry = _model_pricing_cache.get(cache_key)
    if entry is not None and time.monotonic() < entry[1]:
        return entry[0]

    if not _has_pool():
        return None

    if _model_pricing_lock is None:
        _model_pricing_lock = asyncio.Lock()

    async with _model_pricing_lock:
        entry = _model_pricing_cache.get(cache_key)
        if entry is not None and time.monotonic() < entry[1]:
            return entry[0]

        try:
            rule = await get_current_pricing_for_model(provider, model, is_byok=is_byok)
            expires = time.monotonic() + settings.pricing_cache_ttl_seconds
            _model_pricing_cache[cache_key] = (rule, expires)

            if (redis := get_redis()) is not None:
                try:
                    rkey = f"teardrop:pricing:{provider}:{model}:{'byok' if is_byok else 'std'}"
                    payload = json.dumps(rule.model_dump(mode="json") if rule else None, default=str)
                    await redis.setex(rkey, settings.pricing_cache_ttl_seconds, payload)
                except Exception as exc:
                    logger.warning("Redis model pricing cache write failed: %s", exc)

            return rule
        except Exception:
            logger.warning("Failed to refresh model pricing cache", exc_info=True)
            if entry is not None:
                return entry[0]
            return None


async def _load_live_pricing() -> PricingRule | None:
    """Loader for the live-pricing TTL cache. Returns None when DB is unset."""
    if not _has_pool():
        return None
    return await get_current_pricing()


async def _load_tool_overrides() -> dict[str, int]:
    """Loader for the tool-pricing-overrides TTL cache. Returns {} when DB is unset."""
    if not _has_pool():
        return {}
    return await get_current_tool_overrides()


_live_pricing_cache: TTLCache[PricingRule] = TTLCache(
    name="pricing",
    redis_key="teardrop:pricing:active",
    ttl_seconds_fn=lambda: get_settings().pricing_cache_ttl_seconds,
    loader=_load_live_pricing,
    serialize=lambda v: json.dumps(v.model_dump(), default=str),
    deserialize=lambda raw: PricingRule(**json.loads(raw)),
    cache_when=lambda v: v is not None,
    stale_default=None,
)

_tool_overrides_cache_obj: TTLCache[dict[str, int]] = TTLCache(
    name="tool overrides",
    redis_key="teardrop:pricing:tool_overrides",
    ttl_seconds_fn=lambda: get_settings().pricing_cache_ttl_seconds,
    loader=_load_tool_overrides,
    serialize=lambda v: json.dumps(v),
    deserialize=lambda raw: json.loads(raw),
    cache_when=lambda v: v is not None,
    stale_default={},
)


async def get_live_pricing() -> PricingRule | None:
    """Return the current pricing rule using a TTL cache."""
    return await _live_pricing_cache.get()


async def get_current_tool_overrides() -> dict[str, int]:
    """Return all tool pricing overrides as a {tool_name: cost_usdc} dict."""
    pool = _get_pool()
    rows = await pool.fetch("SELECT tool_name, cost_usdc FROM tool_pricing_overrides")
    return {row["tool_name"]: row["cost_usdc"] for row in rows}


async def get_tool_pricing_overrides() -> dict[str, int]:
    """Return tool pricing overrides using a TTL cache."""
    result = await _tool_overrides_cache_obj.get()
    return result if result is not None else {}


async def upsert_tool_pricing_override(tool_name: str, cost_usdc: int, description: str) -> None:
    """Insert or update a tool pricing override and invalidate the cache."""
    pool = _get_pool()
    await pool.execute(
        """
        INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
        VALUES ($1, $2, $3)
        ON CONFLICT (tool_name) DO UPDATE
            SET cost_usdc = EXCLUDED.cost_usdc,
                description = EXCLUDED.description,
                updated_at = NOW()
        """,
        tool_name,
        cost_usdc,
        description,
    )
    await _tool_overrides_cache_obj.invalidate()


async def delete_tool_pricing_override(tool_name: str) -> bool:
    """Delete a tool pricing override. Returns True if a row was deleted."""
    pool = _get_pool()
    result = await pool.execute("DELETE FROM tool_pricing_overrides WHERE tool_name = $1", tool_name)
    deleted = result.split()[-1] != "0"
    await _tool_overrides_cache_obj.invalidate()
    return deleted


async def resolve_tool_cost(
    tool_name: str,
    overrides: dict[str, int],
    default_cost: int,
    marketplace_enabled: bool,
) -> int:
    """Resolve the per-call cost for a tool."""
    if tool_name in overrides:
        return overrides[tool_name]

    if "/" in tool_name:
        _, bare_tool_name = tool_name.split("/", 1)
        if bare_tool_name in overrides:
            return overrides[bare_tool_name]
        if marketplace_enabled:
            # Lazy import: marketplace imports from billing at module init.
            from marketplace import get_org_tool_price_by_qualified_name

            author_price = await get_org_tool_price_by_qualified_name(tool_name)
            if author_price is not None:
                return author_price
        return default_cost

    if marketplace_enabled:
        # Lazy import: marketplace imports from billing at module init.
        from marketplace import get_platform_tool_price

        platform_price = await get_platform_tool_price(tool_name)
        if platform_price is not None:
            return platform_price
    return default_cost


async def calculate_run_cost_usdc(usage_data: dict, provider: str = "", model: str = "") -> int:
    """Calculate the cost of a completed run in atomic USDC (6-decimal integer)."""
    if provider and model:
        rule = await get_live_pricing_for_model(provider, model)
    else:
        rule = await get_live_pricing()
    if rule is None:
        return 0

    tokens_in = int(usage_data.get("tokens_in", 0))
    tokens_out = int(usage_data.get("tokens_out", 0))
    tool_calls = int(usage_data.get("billable_tool_calls", usage_data.get("tool_calls", 0)))
    tool_names: list[str] = usage_data.get("billable_tool_names") or usage_data.get("tool_names") or []

    has_per_unit_rates = rule.tokens_in_cost_per_1k > 0 or rule.tokens_out_cost_per_1k > 0 or rule.tool_call_cost > 0

    if not has_per_unit_rates:
        # Flat-rate rule: every run costs run_price_usdc.
        return rule.run_price_usdc

    token_cost = (tokens_in // 1000) * rule.tokens_in_cost_per_1k + (tokens_out // 1000) * rule.tokens_out_cost_per_1k

    if tool_names:
        overrides = await get_tool_pricing_overrides()
        marketplace_enabled = get_settings().marketplace_enabled
        named_cost = 0
        for name in tool_names:
            named_cost += await resolve_tool_cost(name, overrides, rule.tool_call_cost, marketplace_enabled)
        unnamed_calls = max(0, tool_calls - len(tool_names))
        tool_cost = named_cost + unnamed_calls * rule.tool_call_cost
    else:
        tool_cost = tool_calls * rule.tool_call_cost

    return token_cost + tool_cost


def reset_pricing_caches() -> None:
    """Reset all in-process pricing caches."""
    global _model_pricing_lock
    _live_pricing_cache.reset()
    _tool_overrides_cache_obj.reset()
    _model_pricing_cache.clear()
    _model_pricing_lock = None
