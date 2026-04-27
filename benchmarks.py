# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Model benchmarks — static metadata + live operational metrics from usage_events.

Provides a public ``GET /models/benchmarks`` endpoint with per-model latency,
cost, and throughput data sourced from Teardrop's own production usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg

from cache import get_redis

logger = logging.getLogger(__name__)

# ─── Static model catalogue ──────────────────────────────────────────────────

MODEL_CATALOGUE: dict[str, dict[str, Any]] = {
    "anthropic:claude-haiku-4-5-20251001": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "display_name": "Claude Haiku 4.5",
        "context_window": 200_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 2,
        "default_latency_ms": 600,
        "knowledge_cutoff": "2025-10",
        "training_cutoff_note": "Training data through October 2025",
        "deprecated": True,
    },
    "anthropic:claude-sonnet-4-20250514": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "display_name": "Claude Sonnet 4",
        "context_window": 200_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 1,
        "default_latency_ms": 1800,
        "knowledge_cutoff": "2025-05",
        "training_cutoff_note": "Training data through May 2025",
        "deprecated": True,
    },
    "openai:gpt-4o-mini": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "display_name": "GPT-4o Mini",
        "context_window": 128_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 2,
        "default_latency_ms": 500,
        "knowledge_cutoff": "2024-07",
        "training_cutoff_note": "Training data through July 2024",
        "deprecated": True,
    },
    "openai:gpt-4o": {
        "provider": "openai",
        "model": "gpt-4o",
        "display_name": "GPT-4o",
        "context_window": 128_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 1,
        "default_latency_ms": 1500,
        "knowledge_cutoff": "2024-04",
        "training_cutoff_note": "Training data through April 2024",
        "deprecated": True,
    },
    "google:gemini-2.0-flash": {
        "provider": "google",
        "model": "gemini-2.0-flash",
        "display_name": "Gemini 2.0 Flash",
        "context_window": 1_000_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 2,
        "default_latency_ms": 400,
        "knowledge_cutoff": "2025-01",
        "training_cutoff_note": "Training data through January 2025",
        "deprecated": True,
    },
    "google:gemini-2.5-pro": {
        "provider": "google",
        "model": "gemini-2.5-pro",
        "display_name": "Gemini 2.5 Pro",
        "context_window": 1_000_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 1,
        "default_latency_ms": 2000,
        "knowledge_cutoff": "2025-01",
        "training_cutoff_note": "Training data through January 2025",
        "deprecated": True,
    },
    # ── New pool (April 2026 refresh) ─────────────────────────────────────
    "openrouter:deepseek/deepseek-v3.2": {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v3.2",
        "display_name": "DeepSeek V3.2 (via OpenRouter / DeepInfra)",
        "context_window": 131_072,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 1,
        "default_latency_ms": 800,
        "knowledge_cutoff": "2025-12",
        "training_cutoff_note": "Training data through December 2025",
    },
    "google:gemini-3-flash-preview": {
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "display_name": "Gemini 3 Flash (Preview)",
        "context_window": 1_000_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 2,
        "default_latency_ms": 350,
        "knowledge_cutoff": "2025-10",
        "training_cutoff_note": "Training data through October 2025",
    },
    "anthropic:claude-sonnet-4-6": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "context_window": 200_000,
        "supports_tools": True,
        "supports_streaming": True,
        "quality_tier": 1,
        "default_latency_ms": 1400,
        "knowledge_cutoff": "2025-10",
        "training_cutoff_note": "Training data through October 2025",
    },
}

# ─── Model spec lookup (used for runtime context injection) ──────────────────

_DEFAULT_MODEL_SPECS: dict[str, Any] = {
    "context_window": 128_000,
    "knowledge_cutoff": "Unknown",
    "training_cutoff_note": "Training cutoff date unknown",
    "supports_tools": True,
    "quality_tier": 1,
}


def get_model_context_specs(provider: str, model: str) -> dict[str, Any]:
    """Return static specs for a provider/model pair from MODEL_CATALOGUE.

    Falls back to ``_DEFAULT_MODEL_SPECS`` if the model is not catalogued.
    Used by ``planner_node`` to inject grounding context into the system prompt.
    """
    key = f"{provider}:{model}"
    entry = MODEL_CATALOGUE.get(key)
    if entry is None:
        return dict(_DEFAULT_MODEL_SPECS)
    return {
        "context_window": entry.get("context_window", _DEFAULT_MODEL_SPECS["context_window"]),
        "knowledge_cutoff": entry.get("knowledge_cutoff", _DEFAULT_MODEL_SPECS["knowledge_cutoff"]),
        "training_cutoff_note": entry.get("training_cutoff_note", _DEFAULT_MODEL_SPECS["training_cutoff_note"]),
        "supports_tools": entry.get("supports_tools", True),
        "quality_tier": entry.get("quality_tier", 1),
    }


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_benchmarks_db(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool


async def close_benchmarks_db() -> None:
    global _pool
    _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Benchmarks DB not initialised")
    return _pool


# ─── Benchmark cache ─────────────────────────────────────────────────────────

_BENCHMARK_CACHE_TTL = 900  # 15 minutes
_benchmark_cache: dict[str, dict[str, Any]] | None = None
_benchmark_cache_expires: float = 0.0
_benchmark_lock: asyncio.Lock | None = None


async def get_model_benchmarks(
    org_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return operational benchmarks keyed by 'provider:model'.

    If *org_id* is given, scopes metrics to that org only (no caching).
    Otherwise uses a global 15-minute TTL cache.
    """
    if org_id:
        return await _query_benchmarks(org_id)
    return await _get_cached_benchmarks()


async def _get_cached_benchmarks() -> dict[str, dict[str, Any]]:
    global _benchmark_cache, _benchmark_cache_expires, _benchmark_lock

    redis = get_redis()

    # Redis path
    if redis is not None:
        try:
            cached = await redis.get("teardrop:benchmarks:global")
            if cached is not None:
                return json.loads(cached)
        except Exception:
            pass

    # In-process fast path
    if _benchmark_cache is not None and time.monotonic() < _benchmark_cache_expires:
        return _benchmark_cache

    if _pool is None:
        return {}

    if _benchmark_lock is None:
        _benchmark_lock = asyncio.Lock()

    async with _benchmark_lock:
        if _benchmark_cache is not None and time.monotonic() < _benchmark_cache_expires:
            return _benchmark_cache

        try:
            benchmarks = await _query_benchmarks()
            _benchmark_cache = benchmarks
            _benchmark_cache_expires = time.monotonic() + _BENCHMARK_CACHE_TTL

            if (redis := get_redis()) is not None:
                try:
                    await redis.setex(
                        "teardrop:benchmarks:global",
                        _BENCHMARK_CACHE_TTL,
                        json.dumps(benchmarks, default=str),
                    )
                except Exception:
                    pass

            return benchmarks
        except Exception:
            logger.warning("Failed to query benchmarks", exc_info=True)
            return _benchmark_cache or {}


async def _query_benchmarks(
    org_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Query live benchmarks from usage_events (last 7 days)."""
    pool = _get_pool()

    where_clause = "WHERE created_at > NOW() - INTERVAL '7 days' AND provider != '' AND model != ''"
    params: list[Any] = []
    if org_id:
        where_clause += " AND org_id = $1"
        params.append(org_id)

    query = f"""
        SELECT provider, model,
               COUNT(*) AS total_runs,
               AVG(duration_ms) AS avg_latency_ms,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_latency_ms,
               AVG(cost_usdc) AS avg_cost_usdc,
               AVG(
                   CASE WHEN duration_ms > 0 AND tokens_out > 0
                        THEN tokens_out::float / duration_ms * 1000
                        ELSE NULL
                   END
               ) AS avg_tokens_per_sec
        FROM usage_events
        {where_clause}
        GROUP BY provider, model
        HAVING COUNT(*) >= 10
    """

    rows = await pool.fetch(query, *params)

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['provider']}:{row['model']}"
        result[key] = {
            "total_runs_7d": row["total_runs"],
            "avg_latency_ms": round(float(row["avg_latency_ms"]), 1) if row["avg_latency_ms"] else None,
            "p95_latency_ms": round(float(row["p95_latency_ms"]), 1) if row["p95_latency_ms"] else None,
            "avg_cost_usdc_per_run": round(float(row["avg_cost_usdc"]), 1) if row["avg_cost_usdc"] else None,
            "avg_tokens_per_sec": round(float(row["avg_tokens_per_sec"]), 1) if row["avg_tokens_per_sec"] else None,
        }

    return result


async def build_benchmarks_response(
    org_id: str | None = None,
) -> dict[str, Any]:
    """Build the full benchmarks response combining catalogue + live data."""
    from billing import get_live_pricing_for_model

    benchmarks = await get_model_benchmarks(org_id)

    models = []
    for key, metadata in MODEL_CATALOGUE.items():
        entry: dict[str, Any] = {**metadata}

        # Attach pricing
        try:
            rule = await get_live_pricing_for_model(metadata["provider"], metadata["model"])
            if rule:
                entry["pricing"] = {
                    "tokens_in_cost_per_1k": rule.tokens_in_cost_per_1k,
                    "tokens_out_cost_per_1k": rule.tokens_out_cost_per_1k,
                    "tool_call_cost": rule.tool_call_cost,
                }
        except Exception:
            pass

        # Attach benchmarks
        bm = benchmarks.get(key)
        if bm:
            entry["benchmarks"] = bm

        models.append(entry)

    return {
        "models": models,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
