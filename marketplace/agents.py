# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Opt-in A2A agent registration and derived public directory."""

from __future__ import annotations

import base64
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from marketplace.context import _get_pool
from shared.db_pool import UniqueViolation
from teardrop.a2a_client import A2AAgentCard, _canonicalize_agent_url, async_validate_url, discover_agent_card
from teardrop.cache import TTLCache
from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_AGENT_DIRECTORY_TTL_SECONDS = 300
_MIN_PUBLIC_CALLERS = 5
_REPUTATION_RECENCY_HALF_LIFE_DAYS = 14.0
_REPUTATION_FRESHNESS_HALF_LIFE_DAYS = 30.0
_REPUTATION_PRIOR_SUCCESSES = 4.0
_REPUTATION_PRIOR_SAMPLE_SIZE = 5.0
_REPUTATION_FRESHNESS_FLOOR = 0.75


def _normalize_agent_url(agent_url: str) -> str:
    return _canonicalize_agent_url(agent_url, require_https=True)


def _message_endpoint_matches(endpoint: str, agent_url: str) -> bool:
    stripped = endpoint.rstrip("/")
    if stripped == "/message:send":
        return True
    try:
        parsed = urlsplit(stripped)
        expected = urlsplit(f"{agent_url}/message:send")
        parsed_port = parsed.port or 443
        expected_port = expected.port or 443
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname
        and expected.hostname
        and parsed.hostname.casefold() == expected.hostname.casefold()
        and parsed_port == expected_port
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/") == expected.path.rstrip("/")
    )


def _card_has_message_endpoint(card: A2AAgentCard, agent_url: str) -> bool:
    extra = card.model_extra or {}
    endpoints = extra.get("endpoints")
    if isinstance(endpoints, dict):
        endpoint = endpoints.get("a2a_message")
        if isinstance(endpoint, str) and _message_endpoint_matches(endpoint, agent_url):
            return True

    interfaces = extra.get("supportedInterfaces")
    if isinstance(interfaces, list):
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            url = interface.get("url")
            if isinstance(url, str) and _message_endpoint_matches(url, agent_url):
                return True
    return False


async def set_agent_registration(org_id: str, agent_url: str) -> dict[str, Any]:
    """Validate and idempotently publish an org's A2A endpoint."""
    normalized_url = _normalize_agent_url(agent_url)
    if await async_validate_url(normalized_url):
        raise ValueError("Agent endpoint failed SSRF validation.")

    settings = get_settings()
    try:
        card = await discover_agent_card(
            normalized_url,
            timeout=min(10, int(settings.a2a_delegation_timeout_seconds)),
            cache_ttl=int(settings.a2a_agent_card_cache_ttl_seconds),
        )
    except Exception as exc:
        logger.warning(
            "a2a_agent_registration_card_check_failed org=%s error_type=%s",
            org_id,
            type(exc).__name__,
        )
        raise ValueError("Agent endpoint did not expose a valid A2A agent card.") from None

    if not _card_has_message_endpoint(card, normalized_url):
        raise ValueError("Agent endpoint does not advertise a compatible A2A message endpoint.")

    now = datetime.now(timezone.utc)
    try:
        row = await _get_pool().fetchrow(
            """
            INSERT INTO a2a_agent_registry (org_id, agent_url, created_at, updated_at)
            VALUES ($1, $2, $3, $3)
            ON CONFLICT (org_id) DO UPDATE
                SET agent_url = EXCLUDED.agent_url,
                    updated_at = CASE
                        WHEN a2a_agent_registry.agent_url IS DISTINCT FROM EXCLUDED.agent_url
                        THEN EXCLUDED.updated_at
                        ELSE a2a_agent_registry.updated_at
                    END
            RETURNING org_id, agent_url, created_at, updated_at
            """,
            org_id,
            normalized_url,
            now,
        )
    except UniqueViolation:
        raise ValueError("Agent endpoint is already registered by another organization.") from None

    if row is None:
        raise RuntimeError("Agent registration was not persisted.")
    await _AGENT_DIRECTORY_CACHE.invalidate()
    return dict(row)


async def get_agent_registration(org_id: str) -> dict[str, Any] | None:
    row = await _get_pool().fetchrow(
        "SELECT org_id, agent_url, created_at, updated_at FROM a2a_agent_registry WHERE org_id = $1",
        org_id,
    )
    return dict(row) if row is not None else None


async def delete_agent_registration(org_id: str) -> None:
    await _get_pool().execute("DELETE FROM a2a_agent_registry WHERE org_id = $1", org_id)
    await _AGENT_DIRECTORY_CACHE.invalidate()


async def _load_agent_directory() -> dict[str, Any]:
    rows = await _get_pool().fetch(
        """
        WITH agent_rows AS (
            SELECT
                r.org_id,
                r.agent_url,
                o.slug AS org_slug,
                o.name AS org_name,
                COUNT(t.id)::INT AS tool_count
            FROM a2a_agent_registry r
            JOIN orgs o ON o.id = r.org_id
            LEFT JOIN org_tools t
                ON t.org_id = r.org_id
               AND t.publish_as_mcp = TRUE
               AND t.is_active = TRUE
            WHERE o.slug <> 'platform'
            GROUP BY r.org_id, r.agent_url, o.slug, o.name
        ),
        eligible_events AS (
            SELECT
                r.org_id AS target_org_id,
                e.org_id AS caller_org_id,
                e.task_status,
                e.created_at,
                EXP(
                    -LN(2.0) * GREATEST(
                        0.0,
                        EXTRACT(EPOCH FROM (NOW() - e.created_at)) / 86400.0
                    ) / $1
                ) AS recency_weight
            FROM a2a_agent_registry r
            JOIN a2a_delegation_events e
                ON rtrim(e.agent_url, '/') = r.agent_url
            WHERE e.org_id IS DISTINCT FROM r.org_id
              AND e.failure_origin <> 'local'
              AND e.task_status <> 'possibly_delivered'
        ),
        agent_stats AS (
            SELECT
                target_org_id,
                COALESCE(SUM(recency_weight) FILTER (WHERE task_status = 'completed'), 0.0)::NUMERIC
                    AS weighted_successes,
                COALESCE(SUM(recency_weight), 0.0)::NUMERIC AS weighted_sample_size,
                COUNT(DISTINCT caller_org_id) FILTER (WHERE caller_org_id <> '')::INT AS unique_caller_count,
                MAX(created_at) AS last_event_at
            FROM eligible_events
            GROUP BY target_org_id
        )
        SELECT
            a.org_id,
            a.agent_url,
            a.org_slug,
            a.org_name,
            a.tool_count,
            s.weighted_successes,
            s.weighted_sample_size,
            s.unique_caller_count,
            s.last_event_at
        FROM agent_rows a
        LEFT JOIN agent_stats s ON s.target_org_id = a.org_id
        ORDER BY a.org_slug ASC
        """,
        _REPUTATION_RECENCY_HALF_LIFE_DAYS,
    )

    generated_at = datetime.now(timezone.utc).isoformat()
    agents: list[dict[str, Any]] = []
    for row in rows:
        agent: dict[str, Any] = {
            "org_slug": str(row["org_slug"]),
            "org_name": str(row["org_name"]),
            "agent_url": str(row["agent_url"]),
            "tool_count": int(row["tool_count"] or 0),
        }
        caller_count = int(row["unique_caller_count"] or 0)
        weighted_sample_size = float(row["weighted_sample_size"] or 0.0)
        if caller_count >= _MIN_PUBLIC_CALLERS and weighted_sample_size > 0:
            weighted_successes = float(row["weighted_successes"] or 0.0)
            success_rate = (weighted_successes + _REPUTATION_PRIOR_SUCCESSES) / (
                weighted_sample_size + _REPUTATION_PRIOR_SAMPLE_SIZE
            )
            confidence = weighted_sample_size / (weighted_sample_size + _REPUTATION_PRIOR_SAMPLE_SIZE)
            last_event_at = row["last_event_at"]
            if last_event_at.tzinfo is None:
                last_event_at = last_event_at.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - last_event_at).total_seconds() / 86400.0)
            freshness = math.exp(-math.log(2.0) * age_days / _REPUTATION_FRESHNESS_HALF_LIFE_DAYS)
            agent.update(
                {
                    "reputation_score": round(
                        success_rate * (_REPUTATION_FRESHNESS_FLOOR + (1 - _REPUTATION_FRESHNESS_FLOOR) * freshness),
                        6,
                    ),
                    "success_rate": round(success_rate, 6),
                    "sample_size": round(weighted_sample_size, 6),
                    "confidence": round(confidence, 6),
                    "unique_caller_count": caller_count,
                }
            )
        else:
            agent.update(
                {
                    "reputation_score": None,
                    "success_rate": None,
                    "sample_size": None,
                    "confidence": None,
                    "unique_caller_count": None,
                }
            )
        agents.append(agent)
    return {"generated_at": generated_at, "agents": agents}


_AGENT_DIRECTORY_CACHE = TTLCache[dict[str, Any]](
    name="agent_directory",
    redis_key="teardrop:marketplace:agent-directory",
    ttl_seconds_fn=lambda: _AGENT_DIRECTORY_TTL_SECONDS,
    loader=_load_agent_directory,
    serialize=json.dumps,
    deserialize=json.loads,
    stale_default={"generated_at": None, "agents": []},
)


async def get_agent_directory() -> dict[str, Any]:
    return await _AGENT_DIRECTORY_CACHE.get() or {"generated_at": None, "agents": []}


async def invalidate_agent_directory_cache() -> None:
    await _AGENT_DIRECTORY_CACHE.invalidate()


def _build_agent_cursor(agent: dict[str, Any]) -> str:
    raw = json.dumps({"org_slug": agent["org_slug"]}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_agent_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        return None
    value = data.get("org_slug") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None
