# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""OpenRouter model catalogue sync.

Periodically pulls the public OpenRouter ``/models`` feed into the
``model_catalogue`` table (prices, context length, tool support, expiry and
``~family-latest`` alias targets) and keeps an in-process snapshot for
synchronous lookups such as planner context specs.

Reference data only: billing continues to read ``pricing_rules``.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from shared.db_pool import PgPool
from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 4_021_120
_FETCH_TIMEOUT_SECONDS = 30.0
# A feed shrinking below this fraction of active rows is treated as a bad
# response: rows are upserted but nothing is marked removed.
_MIN_FEED_FRACTION_FOR_REMOVALS = 0.5

_pool: PgPool | None = None
_snapshot: dict[str, dict[str, Any]] = {}


async def init_model_catalogue_db(pool: PgPool) -> None:
    global _pool
    _pool = pool


async def close_model_catalogue_db() -> None:
    global _pool
    _pool = None


# ─── Parsing ──────────────────────────────────────────────────────────────────


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_model(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one OpenRouter ``/models`` entry; returns None if unusable."""
    or_id = raw.get("id")
    if not isinstance(or_id, str) or not or_id:
        return None
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    params = raw.get("supported_parameters") if isinstance(raw.get("supported_parameters"), list) else []
    alias = raw.get("alias_target")
    alias_target = alias.get("slug") if isinstance(alias, dict) else None
    context_length = raw.get("context_length")
    return {
        "or_id": or_id,
        "canonical_slug": str(raw.get("canonical_slug") or ""),
        "name": str(raw.get("name") or ""),
        "context_length": int(context_length) if isinstance(context_length, (int, float)) else None,
        "prompt_price_usd": _to_decimal(pricing.get("prompt")),
        "completion_price_usd": _to_decimal(pricing.get("completion")),
        "pricing": pricing,
        "supported_parameters": params,
        "supports_tools": "tools" in params,
        "expiration_date": _to_date(raw.get("expiration_date")),
        "alias_target": alias_target or None,
    }


async def fetch_openrouter_models(url: str | None = None) -> list[dict[str, Any]]:
    """Fetch and parse the OpenRouter models feed."""
    url = url or get_settings().model_catalogue_url
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json().get("data")
    if not isinstance(data, list):
        raise ValueError("OpenRouter /models response missing 'data' list")
    return [m for m in (parse_model(r) for r in data if isinstance(r, dict)) if m is not None]


# ─── Persistence ─────────────────────────────────────────────────────────────

_UPSERT_SQL = """
    INSERT INTO model_catalogue (
        or_id, canonical_slug, name, context_length, prompt_price_usd,
        completion_price_usd, pricing, supported_parameters, supports_tools,
        expiration_date, alias_target, fetched_at, removed_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11, NOW(), NULL)
    ON CONFLICT (or_id) DO UPDATE SET
        canonical_slug = EXCLUDED.canonical_slug,
        name = EXCLUDED.name,
        context_length = EXCLUDED.context_length,
        prompt_price_usd = EXCLUDED.prompt_price_usd,
        completion_price_usd = EXCLUDED.completion_price_usd,
        pricing = EXCLUDED.pricing,
        supported_parameters = EXCLUDED.supported_parameters,
        supports_tools = EXCLUDED.supports_tools,
        expiration_date = EXCLUDED.expiration_date,
        alias_target = EXCLUDED.alias_target,
        fetched_at = NOW(),
        removed_at = NULL
"""


async def _is_fresh(pool: PgPool, max_age_seconds: float) -> bool:
    return bool(
        await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM model_catalogue WHERE fetched_at > NOW() - make_interval(secs => $1))",
            float(max_age_seconds),
        )
    )


async def store_models(pool: PgPool, models: list[dict[str, Any]]) -> dict[str, int]:
    """Upsert *models*, log alias retargets and mark vanished rows removed.

    Serialised across instances with a transaction-scoped advisory lock;
    returns zero counts if another instance holds it.
    """
    stats = {"upserted": 0, "alias_changes": 0, "removed": 0}
    if not models:
        return stats
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY):
                return stats

            existing = await conn.fetch("SELECT or_id, alias_target FROM model_catalogue WHERE removed_at IS NULL")
            previous_alias = {r["or_id"]: r["alias_target"] for r in existing}

            alias_events = [
                (m["or_id"], previous_alias[m["or_id"]], m["alias_target"])
                for m in models
                if m["or_id"] in previous_alias and previous_alias[m["or_id"]] != m["alias_target"]
            ]

            await conn.executemany(
                _UPSERT_SQL,
                [
                    (
                        m["or_id"],
                        m["canonical_slug"],
                        m["name"],
                        m["context_length"],
                        m["prompt_price_usd"],
                        m["completion_price_usd"],
                        json.dumps(m["pricing"]),
                        json.dumps(m["supported_parameters"]),
                        m["supports_tools"],
                        m["expiration_date"],
                        m["alias_target"],
                    )
                    for m in models
                ],
            )
            stats["upserted"] = len(models)

            if alias_events:
                await conn.executemany(
                    "INSERT INTO model_alias_events (alias, old_target, new_target) VALUES ($1, $2, $3)",
                    alias_events,
                )
                stats["alias_changes"] = len(alias_events)
                for alias, old, new in alias_events:
                    logger.warning("Model alias retargeted: %s %s -> %s", alias, old, new)

            if len(models) >= len(previous_alias) * _MIN_FEED_FRACTION_FOR_REMOVALS:
                status = await conn.execute(
                    "UPDATE model_catalogue SET removed_at = NOW() WHERE removed_at IS NULL AND NOT (or_id = ANY($1))",
                    [m["or_id"] for m in models],
                )
                count = status.rsplit(" ", 1)[-1]
                stats["removed"] = int(count) if count.isdigit() else 0
            else:
                logger.warning(
                    "Model catalogue feed shrank to %d of %d active rows; skipping removals",
                    len(models),
                    len(previous_alias),
                )
    return stats


# ─── Snapshot (sync lookups) ─────────────────────────────────────────────────


async def load_snapshot() -> int:
    """Reload the in-process snapshot of active catalogue rows from Postgres."""
    global _snapshot
    if _pool is None:
        return 0
    rows = await _pool.fetch(
        "SELECT or_id, name, context_length, supports_tools, expiration_date, alias_target"
        " FROM model_catalogue WHERE removed_at IS NULL"
    )
    _snapshot = {r["or_id"]: dict(r) for r in rows}
    return len(_snapshot)


def get_synced_model(provider: str, model: str) -> dict[str, Any] | None:
    """Return the synced catalogue row for an OpenRouter model, if known."""
    if provider != "openrouter":
        return None
    return _snapshot.get(model)


# ─── Sync entry point ────────────────────────────────────────────────────────


async def sync_model_catalogue_once(*, force: bool = False) -> dict[str, int] | None:
    """Fetch OpenRouter models and store them unless a recent sync exists.

    Always refreshes the local snapshot afterwards. Returns write stats, or
    None when the fetch was skipped because another instance synced recently.
    """
    if _pool is None:
        return None
    interval = get_settings().model_catalogue_sync_interval_seconds
    stats: dict[str, int] | None = None
    if force or not await _is_fresh(_pool, interval / 2):
        models = await fetch_openrouter_models()
        stats = await store_models(_pool, models)
    await load_snapshot()
    return stats
