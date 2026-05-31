# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Smart LLM routing — provider cooldowns and cost/speed/quality selection.

Resolves the final LLM config for an agent run, applying routing preferences
against Teardrop's default model pool.  Storage / CRUD live in
:mod:`teardrop.llm_config.base`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from teardrop.config import get_settings
from teardrop.llm_config.base import (
    _resolve_shared_key,
    build_llm_config_dict,
    get_org_llm_config_cached,
)

logger = logging.getLogger(__name__)


# ─── Smart routing ────────────────────────────────────────────────────────────

# Provider cooldown tracking (simple in-process dict of last-failure timestamps)
_provider_cooldowns: dict[str, float] = {}
_COOLDOWN_SECONDS = 60.0

# Static quality tiers for quality-based routing.
# Tier 1 = premium/quality, Tier 2 = standard/cost.  Models absent here fall
# back to tier 99 (lowest priority) in _select_highest_quality().
_QUALITY_TIERS: dict[str, int] = {
    "deepseek/deepseek-v4-flash": 2,
    "gemini-3-flash-preview": 2,
    "claude-sonnet-4-6": 1,
}


def record_provider_failure(provider: str, model: str) -> None:
    """Mark a provider+model as temporarily failed (cooldown)."""
    _provider_cooldowns[f"{provider}:{model}"] = time.monotonic()


def is_provider_cooled_down(provider: str, model: str) -> bool:
    """Check if a provider+model is in cooldown."""
    last_failure = _provider_cooldowns.get(f"{provider}:{model}")
    if last_failure is None:
        return False
    return (time.monotonic() - last_failure) < _COOLDOWN_SECONDS


async def resolve_llm_config(
    org_id: str,
    routing_preference: str | None = None,
) -> dict[str, Any] | None:
    """Resolve the final LLM config dict for an agent run.

    Applies smart routing when ``routing_preference`` is cost/speed/quality.
    BYOK orgs always use their own config (no routing across Teardrop's pool).

    Returns ``None`` if the org has no config and routing is default (use global).
    """
    cfg = await get_org_llm_config_cached(org_id)

    if cfg is None:
        # No org config — check if routing was explicitly requested
        if routing_preference and routing_preference != "default":
            return await _route_from_pool(routing_preference)
        return None

    effective_routing = routing_preference or cfg.routing_preference

    # BYOK orgs always use their own model — no smart routing across Teardrop pool
    if cfg.is_byok or effective_routing == "default":
        return await build_llm_config_dict(org_id)

    return await _route_from_pool(effective_routing)


async def _route_from_pool(routing_preference: str) -> dict[str, Any] | None:
    """Select a model from Teardrop's default pool based on routing strategy."""
    settings = get_settings()
    pool_models = settings.default_model_pool

    if not pool_models:
        return None

    # Filter out cooled-down providers
    available = [m for m in pool_models if not is_provider_cooled_down(m["provider"], m["model"])]
    if not available:
        # All cooled down — use first anyway (best effort)
        available = pool_models

    if routing_preference == "cost":
        selected = await _select_cheapest(available)
    elif routing_preference == "speed":
        selected = await _select_fastest(available)
    elif routing_preference == "quality":
        selected = _select_highest_quality(available)
    else:
        selected = available[0]

    # Build config dict using shared keys
    return {
        "provider": selected["provider"],
        "model": selected["model"],
        "api_key": _resolve_shared_key(selected["provider"], settings),
        "api_base": None,
        "max_tokens": settings.agent_max_tokens,
        "temperature": settings.agent_temperature,
        "timeout_seconds": settings.agent_llm_timeout_seconds,
    }


async def _select_cheapest(models: list[dict[str, str]]) -> dict[str, str]:
    """Select the cheapest model based on pricing rules."""
    from billing import get_live_pricing_for_model

    best = models[0]
    best_cost = float("inf")
    for m in models:
        try:
            rule = await get_live_pricing_for_model(m["provider"], m["model"])
            if rule is not None:
                cost = rule.tokens_in_cost_per_1k + rule.tokens_out_cost_per_1k
                if cost < best_cost:
                    best_cost = cost
                    best = m
        except Exception:
            continue
    return best


async def _select_fastest(models: list[dict[str, str]]) -> dict[str, str]:
    """Select the fastest model by p95 latency (live benchmarks) with static fallback."""
    from teardrop.benchmarks import MODEL_CATALOGUE, get_model_benchmarks

    try:
        live = await get_model_benchmarks()
    except Exception:
        logger.warning(
            "_select_fastest: failed to query benchmarks, using static fallback",
            exc_info=True,
        )
        live = {}

    best = models[0]
    best_latency = float("inf")

    for m in models:
        key = f"{m['provider']}:{m['model']}"
        bm = live.get(key, {})
        # Priority: live p95 → live avg → static catalogue default
        latency = (
            bm.get("p95_latency_ms")
            or bm.get("avg_latency_ms")
            or (MODEL_CATALOGUE.get(key) or {}).get("default_latency_ms")
            or float("inf")
        )
        if latency < best_latency:
            best_latency = latency
            best = m

    best_key = f"{best['provider']}:{best['model']}"
    best_bm = live.get(best_key, {})
    if best_bm.get("p95_latency_ms"):
        source = "live_p95"
    elif best_bm.get("avg_latency_ms"):
        source = "live_avg"
    else:
        source = "static"
    logger.debug(
        "_select_fastest: selected %s/%s latency=%.1f source=%s candidates=%d",
        best["provider"],
        best["model"],
        best_latency,
        source,
        len(models),
    )
    return best


def _select_highest_quality(models: list[dict[str, str]]) -> dict[str, str]:
    """Select the highest quality model based on static quality tiers."""
    best = models[0]
    best_tier = _QUALITY_TIERS.get(best["model"], 99)
    for m in models[1:]:
        tier = _QUALITY_TIERS.get(m["model"], 99)
        if tier < best_tier:
            best_tier = tier
            best = m
    return best
