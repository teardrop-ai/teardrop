# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Per-org LLM configuration — CRUD, caching, encryption, and routing resolution.

Allows organisations to configure their preferred LLM provider/model, bring
their own API keys (BYOK), set routing preferences, and optionally point at
OpenAI-compatible self-hosted endpoints (vLLM, Ollama, OpenRouter, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet
from pydantic import BaseModel, Field

from cache import get_redis
from config import get_settings

logger = logging.getLogger(__name__)


# ─── LLM-specific encryption (separate key from org-tools) ───────────────────

_llm_fernet: Fernet | None = None


def _get_llm_fernet() -> Fernet:
    """Return a Fernet instance using the LLM-specific encryption key.

    Falls back to ``org_tool_encryption_key`` if ``llm_config_encryption_key``
    is not set, providing backward compatibility for existing deployments.
    """
    global _llm_fernet
    if _llm_fernet is not None:
        return _llm_fernet
    settings = get_settings()
    key = settings.llm_config_encryption_key or settings.org_tool_encryption_key
    if not key:
        raise RuntimeError(
            "Neither LLM_CONFIG_ENCRYPTION_KEY nor ORG_TOOL_ENCRYPTION_KEY is set. Cannot encrypt/decrypt BYOK API keys."
        )
    _llm_fernet = Fernet(key.encode())
    return _llm_fernet


def _encrypt_llm_key(value: str) -> str:
    """Encrypt an LLM API key for at-rest storage."""
    return _get_llm_fernet().encrypt(value.encode()).decode()


def _decrypt_llm_key(encrypted: str) -> str:
    """Decrypt an LLM API key from at-rest storage."""
    return _get_llm_fernet().decrypt(encrypted.encode()).decode()


def reset_llm_fernet() -> None:
    """Clear the cached Fernet instance (used by tests)."""
    global _llm_fernet
    _llm_fernet = None


# ─── Models ───────────────────────────────────────────────────────────────────

ALLOWED_ROUTING_PREFERENCES = frozenset({"default", "cost", "speed", "quality"})


class OrgLlmConfig(BaseModel):
    """Public representation of an org's LLM configuration.

    ``has_api_key`` is a boolean flag — the raw key is never exposed.
    """

    org_id: str
    provider: str
    model: str
    has_api_key: bool = False
    api_base: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout_seconds: int = 120
    routing_preference: str = "default"
    is_byok: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Database pool ────────────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def init_llm_config_db(pool: asyncpg.Pool) -> None:
    """Store the asyncpg pool reference.  Called during app lifespan startup."""
    global _pool
    _pool = pool
    logger.info("LLM config DB ready")


async def close_llm_config_db() -> None:
    """Release the pool reference."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("LLM config DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("LLM config DB not initialised — call init_llm_config_db() first")
    return _pool


# ─── TTL cache ────────────────────────────────────────────────────────────────

_config_cache: dict[str, tuple[OrgLlmConfig | None, float]] = {}  # org_id -> (config, expires)
_config_lock: asyncio.Lock | None = None


def _get_cache_ttl() -> int:
    return get_settings().org_tools_cache_ttl_seconds  # reuse existing TTL setting


async def invalidate_llm_config_cache(org_id: str) -> None:
    """Remove a cached org config entry (after upsert / delete)."""
    _config_cache.pop(org_id, None)
    redis = get_redis()
    if redis is not None:
        try:
            await redis.delete(f"teardrop:llm_config:{org_id}")
        except Exception as exc:
            logger.warning("Redis LLM config cache invalidation failed (non-fatal): %s", exc)


# ─── CRUD ─────────────────────────────────────────────────────────────────────


def _row_to_config(row: asyncpg.Record) -> OrgLlmConfig:
    """Map a DB row to an ``OrgLlmConfig`` model."""
    return OrgLlmConfig(
        org_id=row["org_id"],
        provider=row["provider"],
        model=row["model"],
        has_api_key=row["api_key_enc"] is not None,
        api_base=row["api_base"],
        max_tokens=row["max_tokens"],
        temperature=row["temperature"],
        timeout_seconds=row["timeout_seconds"],
        routing_preference=row["routing_preference"],
        is_byok=row["is_byok"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_org_llm_config(org_id: str) -> OrgLlmConfig | None:
    """Fetch the LLM config for an org.  Returns ``None`` if not configured."""
    pool = _get_pool()
    row = await pool.fetchrow("SELECT * FROM org_llm_config WHERE org_id = $1", org_id)
    if row is None:
        return None
    return _row_to_config(row)


async def get_org_llm_config_cached(org_id: str) -> OrgLlmConfig | None:
    """Return org LLM config with TTL caching (Redis → in-process → DB)."""
    global _config_lock
    redis = get_redis()
    ttl = _get_cache_ttl()

    # Redis path
    if redis is not None:
        try:
            key = f"teardrop:llm_config:{org_id}"
            cached_json = await redis.get(key)
            if cached_json is not None:
                data = json.loads(cached_json)
                if data is None:
                    return None
                return OrgLlmConfig(**data)
        except Exception as exc:
            logger.warning("Redis LLM config cache read failed; falling back: %s", exc)

    # In-process fast path
    entry = _config_cache.get(org_id)
    if entry is not None and time.monotonic() < entry[1]:
        return entry[0]

    if _pool is None:
        return None

    if _config_lock is None:
        _config_lock = asyncio.Lock()

    async with _config_lock:
        # Double-check after lock
        entry = _config_cache.get(org_id)
        if entry is not None and time.monotonic() < entry[1]:
            return entry[0]

        try:
            config = await get_org_llm_config(org_id)
            expires = time.monotonic() + ttl
            _config_cache[org_id] = (config, expires)

            if (redis := get_redis()) is not None:
                try:
                    cache_key = f"teardrop:llm_config:{org_id}"
                    payload = json.dumps(config.model_dump(mode="json") if config else None, default=str)
                    await redis.setex(cache_key, ttl, payload)
                except Exception as exc:
                    logger.warning("Redis LLM config cache write failed (non-fatal): %s", exc)

            return config
        except Exception:
            logger.warning("Failed to refresh LLM config cache for org %s", org_id, exc_info=True)
            if entry is not None:
                return entry[0]
            return None


async def upsert_org_llm_config(
    org_id: str,
    *,
    provider: str,
    model: str,
    api_key: str | None = None,
    clear_api_key: bool = False,
    api_base: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout_seconds: int = 120,
    routing_preference: str = "default",
) -> OrgLlmConfig:
    """Insert or update an org's LLM configuration.

    If *api_key* is provided, it is encrypted at rest via Fernet.
    If *api_key* is ``None`` and *clear_api_key* is ``False``, the existing
    key is preserved on update.
    If *api_key* is ``None`` and *clear_api_key* is ``True``, any stored key
    is removed and ``is_byok`` is set to ``False``.
    """
    pool = _get_pool()
    is_byok = api_key is not None

    api_key_enc: str | None = None
    if api_key is not None:
        api_key_enc = _encrypt_llm_key(api_key)

    now = datetime.now(timezone.utc)

    if api_key_enc is not None:
        # Full upsert including API key
        await pool.execute(
            """
            INSERT INTO org_llm_config
                (org_id, provider, model, api_key_enc, api_base,
                 max_tokens, temperature, timeout_seconds,
                 routing_preference, is_byok, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
            ON CONFLICT (org_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                api_key_enc = EXCLUDED.api_key_enc,
                api_base = EXCLUDED.api_base,
                max_tokens = EXCLUDED.max_tokens,
                temperature = EXCLUDED.temperature,
                timeout_seconds = EXCLUDED.timeout_seconds,
                routing_preference = EXCLUDED.routing_preference,
                is_byok = EXCLUDED.is_byok,
                updated_at = EXCLUDED.updated_at
            """,
            org_id,
            provider,
            model,
            api_key_enc,
            api_base,
            max_tokens,
            temperature,
            timeout_seconds,
            routing_preference,
            is_byok,
            now,
        )
    elif clear_api_key:
        # Explicitly clear BYOK key while preserving other config
        await pool.execute(
            """
            INSERT INTO org_llm_config
                (org_id, provider, model, api_key_enc, api_base,
                 max_tokens, temperature, timeout_seconds,
                 routing_preference, is_byok, created_at, updated_at)
            VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, FALSE, $9, $9)
            ON CONFLICT (org_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                api_key_enc = NULL,
                api_base = EXCLUDED.api_base,
                max_tokens = EXCLUDED.max_tokens,
                temperature = EXCLUDED.temperature,
                timeout_seconds = EXCLUDED.timeout_seconds,
                routing_preference = EXCLUDED.routing_preference,
                is_byok = FALSE,
                updated_at = EXCLUDED.updated_at
            """,
            org_id,
            provider,
            model,
            api_base,
            max_tokens,
            temperature,
            timeout_seconds,
            routing_preference,
            now,
        )
        is_byok = False
        has_key = False
    else:
        # Upsert preserving existing api_key_enc — use RETURNING to
        # reflect actual DB state in the returned object.
        row = await pool.fetchrow(
            """
            INSERT INTO org_llm_config
                (org_id, provider, model, api_base,
                 max_tokens, temperature, timeout_seconds,
                 routing_preference, is_byok, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, FALSE, $9, $9)
            ON CONFLICT (org_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                api_base = EXCLUDED.api_base,
                max_tokens = EXCLUDED.max_tokens,
                temperature = EXCLUDED.temperature,
                timeout_seconds = EXCLUDED.timeout_seconds,
                routing_preference = EXCLUDED.routing_preference,
                updated_at = EXCLUDED.updated_at
            RETURNING is_byok, (api_key_enc IS NOT NULL) AS has_api_key
            """,
            org_id,
            provider,
            model,
            api_base,
            max_tokens,
            temperature,
            timeout_seconds,
            routing_preference,
            now,
        )
        is_byok = row["is_byok"] if row else False
        has_key = row["has_api_key"] if row else False

    # For the api_key branch, both flags are trivially True.
    if api_key_enc is not None:
        has_key = True

    await invalidate_llm_config_cache(org_id)

    return OrgLlmConfig(
        org_id=org_id,
        provider=provider,
        model=model,
        has_api_key=has_key,
        api_base=api_base,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        routing_preference=routing_preference,
        is_byok=is_byok,
        created_at=now,
        updated_at=now,
    )


async def delete_org_llm_config(org_id: str) -> bool:
    """Delete an org's LLM configuration.  Returns ``True`` if a row was deleted."""
    pool = _get_pool()
    result = await pool.execute("DELETE FROM org_llm_config WHERE org_id = $1", org_id)
    deleted = result.split()[-1] != "0"
    await invalidate_llm_config_cache(org_id)
    return deleted


# ─── Config → LLM dict builder ───────────────────────────────────────────────


async def build_llm_config_dict(org_id: str) -> dict[str, Any] | None:
    """Build the config dict consumed by ``create_llm_from_config()``.

    Resolves the API key: decrypts the org's BYOK key if present, otherwise
    falls back to the global key from settings for the configured provider.

    Returns ``None`` if the org has no LLM config (use global defaults).
    """
    pool = _get_pool()
    row = await pool.fetchrow("SELECT * FROM org_llm_config WHERE org_id = $1", org_id)
    if row is None:
        return None

    settings = get_settings()

    # Resolve API key: BYOK (encrypted in DB) or shared (from settings)
    api_key = ""
    if row["api_key_enc"]:
        try:
            api_key = _decrypt_llm_key(row["api_key_enc"])
        except Exception:
            if row["is_byok"]:
                # BYOK key is corrupted/unreadable — do NOT silently fall back
                # to Teardrop's shared key (that would bill Teardrop, not the org).
                logger.error(
                    "BYOK API key decryption failed for org %s — org must re-upload key",
                    org_id,
                )
                raise RuntimeError(
                    f"BYOK API key could not be decrypted for org {org_id}. Please re-upload your API key via PUT /llm-config."
                )
            logger.warning("API key decryption failed for org %s — falling back to shared key", org_id)
            api_key = _resolve_shared_key(row["provider"], settings)
    else:
        api_key = _resolve_shared_key(row["provider"], settings)

    return {
        "provider": row["provider"],
        "model": row["model"],
        "api_key": api_key,
        "api_base": row["api_base"],
        "max_tokens": row["max_tokens"],
        "temperature": float(row["temperature"]),
        "timeout_seconds": row["timeout_seconds"],
    }


def _resolve_shared_key(provider: str, settings: Any) -> str:
    """Return the platform's shared API key for a provider."""
    mapping = {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "google": settings.google_api_key,
        "openrouter": settings.openrouter_api_key,
    }
    return mapping.get(provider, "")


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
    from benchmarks import MODEL_CATALOGUE, get_model_benchmarks

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
