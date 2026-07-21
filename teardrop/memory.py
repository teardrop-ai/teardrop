# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Persistent agent memory data layer (async Postgres + pgvector).

Provides:
- MemoryEntry model
- init_memory_db()              — register pgvector types on startup
- store_memory()                — embed + INSERT (fire-and-forget safe)
- recall_memories()             — cosine-similarity search over org memories
- extract_and_store_memories()  — LLM-based fact + decision extraction, batch store
- list_memories()               — cursor-paginated listing
- delete_memory()               — org-scoped single delete
- delete_all_org_memories()     — admin purge
- count_memories()              — count per org
- store_run_decision()          — persist one decision-graph record per run
- list_run_decisions()          — cursor-paginated decision listing
- backfill_decision_outcome()   — attach a ground-truth outcome label
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from teardrop.config import get_settings

logger = logging.getLogger(__name__)

# ─── Models ───────────────────────────────────────────────────────────────────


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    org_id: str
    user_id: str
    content: str
    source_run_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Database initialisation ─────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None
_memory_count_cache: dict[str, tuple[float, bool]] = {}
_MEMORY_COUNT_CACHE_TTL = 60

_STATELESS_TOOLS: frozenset[str] = frozenset(
    {
        "get_token_price",
        "get_token_price_historical",
        "get_gas_price",
        "get_datetime",
        "convert_currency",
        "get_lending_rates",
        "get_protocol_tvl",
        "get_yield_rates",
    }
)
_WALLET_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")

# Decision-graph slot snapshot: only non-identifying market and protocol
# summaries are persisted. Wallet-keyed balances and positions remain in the
# checkpoint state and are never copied into long-lived run telemetry.
_SLOTS_SNAPSHOT_ALLOWLIST: frozenset[str] = frozenset({"prices", "rates", "tvl"})
_SLOTS_SNAPSHOT_MAX_BYTES = 8192
RUN_DECISION_SCHEMA_VERSION = 1
TASK_CLASS_TAXONOMY_VERSION = 1


async def init_memory_db(pool: asyncpg.Pool) -> None:
    """Store pool reference and register pgvector types for VECTOR columns."""
    global _pool
    _pool = pool
    settings = get_settings()

    if not settings.memory_enabled:
        logger.info("Memory disabled via config")
        return

    if not settings.openai_api_key:
        logger.warning("Memory disabled — openai_api_key is not set")
        return

    logger.info("Memory DB ready (pgvector types registered per-connection via pool init)")


async def close_memory_db() -> None:
    """Release the pool reference (pool is closed by the caller)."""
    global _pool
    if _pool is not None:
        _pool = None
        logger.info("Memory DB reference released")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Memory DB not initialised — call init_memory_db() first")
    return _pool


# ─── Embedding generation ────────────────────────────────────────────────────

_openai_client: Any = None


def _get_openai_client() -> Any:
    """Return a cached OpenAI client instance."""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        settings = get_settings()
        _openai_client = OpenAI(api_key=settings.openai_api_key)
    return _openai_client


async def _generate_embedding(text: str) -> list[float]:
    """Generate an embedding vector for the given text via OpenAI.

    Runs the synchronous OpenAI SDK call in a thread to avoid blocking the
    event loop.
    """
    import asyncio

    settings = get_settings()
    client = _get_openai_client()

    def _call() -> list[float]:
        response = client.embeddings.create(
            model=settings.embedding_model,
            input=[text],
        )
        return response.data[0].embedding

    return await asyncio.get_event_loop().run_in_executor(None, _call)


async def has_memories_cached(org_id: str) -> bool:
    """Return whether the org likely has memories, using a short-lived cache."""
    now = time.monotonic()
    cached = _memory_count_cache.get(org_id)
    if cached and now < cached[0]:
        return cached[1]

    try:
        has_memories = await count_memories(org_id) > 0
    except Exception:
        # Fail-open to avoid accidentally skipping recall when DB is transiently unavailable.
        return True

    _memory_count_cache[org_id] = (now + _MEMORY_COUNT_CACHE_TTL, has_memories)
    return has_memories


def _is_stateless_lookup_run(messages: list[Any], tool_names_used: list[str]) -> bool:
    """Conservatively classify simple lookup runs where memory extraction adds little value."""
    if any(name not in _STATELESS_TOOLS for name in tool_names_used):
        return False

    for msg in messages:
        if getattr(msg, "type", "") != "human":
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str) and _WALLET_ADDRESS_RE.search(content):
            return False

    return True


# ─── Write ────────────────────────────────────────────────────────────────────


async def store_memory(
    org_id: str,
    user_id: str,
    content: str,
    source_run_id: str | None = None,
) -> MemoryEntry | None:
    """Embed and store a single memory. Returns None on failure or duplicate.

    Enforces max_memories_per_org limit. Deduplicates via content_hash.
    Sets expires_at if memory_ttl_days > 0. Never raises — logs errors instead.
    """
    try:
        settings = get_settings()
        pool = _get_pool()

        # Enforce per-org limit.
        current_count = await count_memories(org_id)
        if current_count >= settings.memory_max_per_org:
            logger.warning(
                "Memory limit reached for org_id=%s (%d/%d)",
                org_id,
                current_count,
                settings.memory_max_per_org,
            )
            return None

        # Truncate content to 500 chars for safety.
        content = content[:500]

        # Compute content hash for deduplication.
        content_hash = hashlib.sha256(content.strip().lower().encode()).hexdigest()

        # Compute expiry if TTL is configured.
        expires_at = None
        if settings.memory_ttl_days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=settings.memory_ttl_days)

        embedding = await _generate_embedding(content)
        entry = MemoryEntry(
            org_id=org_id,
            user_id=user_id,
            content=content,
            source_run_id=source_run_id,
        )

        result = await pool.fetchrow(
            """
            INSERT INTO org_memories
                (id, org_id, user_id, content, embedding, source_run_id,
                 content_hash, expires_at, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (org_id, content_hash) WHERE content_hash IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            entry.id,
            entry.org_id,
            entry.user_id,
            entry.content,
            embedding,
            entry.source_run_id,
            content_hash,
            expires_at,
            entry.created_at,
        )

        if result is None:
            logger.debug("Duplicate memory skipped for org_id=%s hash=%s", org_id, content_hash[:8])
            return None

        _memory_count_cache[org_id] = (time.monotonic() + _MEMORY_COUNT_CACHE_TTL, True)

        return entry

    except Exception:
        logger.exception("Failed to store memory for org_id=%s", org_id)
        return None


# ─── Read ─────────────────────────────────────────────────────────────────────


async def recall_memories(
    org_id: str,
    query_text: str,
    top_k: int = 5,
) -> list[MemoryEntry]:
    """Retrieve the top-K most relevant memories for a query.

    Returns an empty list on any error — memory retrieval must never block agent runs.
    """
    try:
        if not await has_memories_cached(org_id):
            return []

        pool = _get_pool()
        query_embedding = await _generate_embedding(query_text)

        rows = await pool.fetch(
            """
            SELECT id, org_id, user_id, content, source_run_id, created_at
            FROM org_memories
            WHERE org_id = $1
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY embedding <=> $2
            LIMIT $3
            """,
            org_id,
            query_embedding,
            top_k,
        )

        return [
            MemoryEntry(
                id=r["id"],
                org_id=r["org_id"],
                user_id=r["user_id"],
                content=r["content"],
                source_run_id=r["source_run_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    except Exception:
        logger.exception("Failed to recall memories for org_id=%s", org_id)
        return []


async def list_memories(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[MemoryEntry]:
    """List memories for an org, ordered newest-first, cursor-paginated."""
    pool = _get_pool()

    if cursor is None:
        rows = await pool.fetch(
            """
            SELECT id, org_id, user_id, content, source_run_id, created_at
            FROM org_memories
            WHERE org_id = $1
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, org_id, user_id, content, source_run_id, created_at
            FROM org_memories
            WHERE org_id = $1 AND created_at < $3
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
            cursor,
        )

    return [
        MemoryEntry(
            id=r["id"],
            org_id=r["org_id"],
            user_id=r["user_id"],
            content=r["content"],
            source_run_id=r["source_run_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def count_memories(org_id: str) -> int:
    """Return the number of memories stored for an org."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) FROM org_memories WHERE org_id = $1",
        org_id,
    )
    return int(row[0]) if row else 0


# ─── Delete ───────────────────────────────────────────────────────────────────


async def delete_memory(memory_id: str, org_id: str) -> bool:
    """Delete a single memory. Returns True if a row was actually deleted.

    Scoped to org_id for tenant isolation — prevents cross-org deletion.
    """
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM org_memories WHERE id = $1 AND org_id = $2",
        memory_id,
        org_id,
    )
    return result == "DELETE 1"


async def delete_all_org_memories(org_id: str) -> int:
    """Delete all memories for an org. Returns count of deleted rows."""
    pool = _get_pool()
    result = await pool.execute(
        "DELETE FROM org_memories WHERE org_id = $1",
        org_id,
    )
    # asyncpg returns e.g. "DELETE 42"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def cleanup_expired_memories() -> int:
    """Delete memories past their TTL. Returns count of deleted rows."""
    pool = _get_pool()
    result = await pool.execute("DELETE FROM org_memories WHERE expires_at IS NOT NULL AND expires_at < NOW()")
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# ─── Decision graph (run_decisions) ──────────────────────────────────────────


def _sanitize_slots_snapshot(slots: dict[str, Any] | None) -> dict[str, Any]:
    """Allowlist-filter slots before persistence.

    Drops any key not in ``_SLOTS_SNAPSHOT_ALLOWLIST`` and refuses to store a
    snapshot larger than ``_SLOTS_SNAPSHOT_MAX_BYTES`` (returns ``{}`` instead
    of truncating mid-structure, which could silently corrupt nested facts).
    """
    if not isinstance(slots, dict):
        return {}
    filtered = {k: v for k, v in slots.items() if k in _SLOTS_SNAPSHOT_ALLOWLIST}
    if not filtered:
        return {}
    try:
        encoded = json.dumps(filtered)
    except (TypeError, ValueError):
        return {}
    if len(encoded) > _SLOTS_SNAPSHOT_MAX_BYTES:
        return {}
    return filtered


async def store_run_decision(
    *,
    org_id: str,
    user_id: str,
    run_id: str,
    decision: dict[str, Any],
    tool_names: list[str] | None = None,
    slots: dict[str, Any] | None = None,
    outcome: int = 0,
    outcome_source: str = "",
) -> bool:
    """Persist one decision-graph record for a run. Returns False on failure or duplicate.

    At most one row per ``run_id`` (``ON CONFLICT DO NOTHING``) — a run
    produces a single decision summary. Never raises — logs and returns
    False on any error, matching :func:`store_memory`'s fire-and-forget
    contract.
    """
    try:
        pool = _get_pool()
        snapshot = _sanitize_slots_snapshot(slots)
        confidence = decision.get("confidence")
        if outcome not in (-1, 0, 1):
            outcome = 0
        if outcome == 0:
            outcome_source = ""
        elif outcome_source not in {"auto", "explicit", "feedback"}:
            outcome_source = ""
        result = await pool.fetchrow(
            """
            INSERT INTO run_decisions
                (id, run_id, org_id, user_id, task_class, action, reasoning,
                  confidence, slots_snapshot, tool_names, outcome, outcome_source, outcome_at,
                  schema_version, taxonomy_version, created_at)
              VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12,
                      CASE WHEN $11 = 0 THEN NULL ELSE NOW() END, $13, $14, $15)
            ON CONFLICT (run_id) DO NOTHING
            RETURNING id
            """,
            str(uuid.uuid4()),
            run_id,
            org_id,
            user_id,
            str(decision.get("task_class", ""))[:60],
            str(decision.get("action", ""))[:120],
            str(decision.get("reasoning", ""))[:500],
            float(confidence) if isinstance(confidence, (int, float)) else None,
            json.dumps(snapshot),
            list(tool_names or []),
            outcome,
            outcome_source,
            RUN_DECISION_SCHEMA_VERSION,
            TASK_CLASS_TAXONOMY_VERSION,
            datetime.now(timezone.utc),
        )
        return result is not None
    except Exception:
        logger.exception("Failed to store run decision for run_id=%s", run_id)
        return False


async def list_run_decisions(
    org_id: str,
    limit: int = 50,
    cursor: datetime | None = None,
) -> list[dict[str, Any]]:
    """List decision records for an org, newest-first, cursor-paginated."""
    pool = _get_pool()

    if cursor is None:
        rows = await pool.fetch(
            """
            SELECT id, run_id, task_class, action, reasoning, confidence,
                   tool_names, outcome, outcome_source, created_at
            FROM run_decisions
            WHERE org_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, run_id, task_class, action, reasoning, confidence,
                   tool_names, outcome, outcome_source, created_at
            FROM run_decisions
            WHERE org_id = $1 AND created_at < $3
            ORDER BY created_at DESC
            LIMIT $2
            """,
            org_id,
            limit,
            cursor,
        )

    return [dict(r) for r in rows]


async def backfill_decision_outcome(run_id: str, org_id: str, rating: int, source: str = "feedback") -> bool:
    """Attach a ground-truth outcome label (-1/0/1) to an existing decision record.

    Org-scoped to prevent cross-org writes. Human-originated sources replace an
    unlabeled or automated result, but never replace another human label.
    Never raises.
    """
    if rating not in (-1, 0, 1) or source not in {"explicit", "feedback"}:
        return False
    try:
        pool = _get_pool()
        result = await pool.execute(
            """
            UPDATE run_decisions
            SET outcome = $3, outcome_source = $4, outcome_at = NOW()
            WHERE run_id = $1 AND org_id = $2
              AND outcome_source IN ('', 'auto')
            """,
            run_id,
            org_id,
            rating,
            source,
        )
        return result == "UPDATE 1"
    except Exception:
        logger.exception("Failed to backfill decision outcome for run_id=%s", run_id)
        return False


# ─── Fact extraction (LLM-based) ─────────────────────────────────────────────

_EXTRACT_FACTS_PROMPT = """\
You are a factual-memory extractor and decision summarizer. Given a conversation between a user and an \
AI assistant, extract two things:

1. Up to 5 key factual statements worth remembering for future interactions. Focus on:
- User preferences, constraints, or recurring requests
- Domain-specific facts the user shared (wallet addresses, project names, etc.)
- Decisions made or conclusions reached

2. A single structured summary of the primary decision or recommendation the assistant made in
this conversation, if any (e.g. a risk flag, a recommendation, an analysis conclusion). Omit it
(use null) if the conversation was a simple lookup with no judgment call.

Rules:
- Each fact must be a single, self-contained sentence (max 500 characters).
- Do NOT include greetings, pleasantries, or meta-commentary.
- If the conversation contains no memorable facts, return an empty list for facts.
- "action" is a short label (max 120 chars), "reasoning" is max 500 chars, "task_class" is a
  short category label (e.g. "liquidation_risk", "portfolio_lookup"), "confidence" is a float 0-1.

Respond with ONLY a JSON object:
{"facts": ["fact one", "fact two", ...], "decision":
  {"action": "...", "reasoning": "...", "task_class": "...", "confidence": 0.8} | null}
"""


def _parse_decision(raw_decision: Any) -> dict[str, Any] | None:
    """Validate and truncate a raw decision object parsed from the LLM response."""
    if not isinstance(raw_decision, dict):
        return None
    action = str(raw_decision.get("action", ""))[:120]
    reasoning = str(raw_decision.get("reasoning", ""))[:500]
    if not action and not reasoning:
        return None
    confidence_raw = raw_decision.get("confidence")
    confidence = max(0.0, min(1.0, float(confidence_raw))) if isinstance(confidence_raw, (int, float)) else None
    return {
        "action": action,
        "reasoning": reasoning,
        "task_class": str(raw_decision.get("task_class", ""))[:60],
        "confidence": confidence,
    }


async def _extract_facts_and_decision(messages: list[Any]) -> tuple[list[str], dict[str, Any] | None]:
    """Use the configured LLM to extract facts and a structured decision summary.

    Single LLM call serves both ``org_memories`` (facts) and ``run_decisions``
    (decision) to avoid doubling per-run LLM cost. Returns ``([], None)`` on
    any error, empty conversation, or unparseable response. Never raises.
    """
    try:
        from agent.llm import get_llm

        # Build a condensed transcript (last 10 messages max to control cost).
        transcript_lines: list[str] = []
        for msg in messages[-10:]:
            role = getattr(msg, "type", "unknown")
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content.strip():
                transcript_lines.append(f"{role}: {content[:1000]}")

        if not transcript_lines:
            return [], None

        transcript = "\n".join(transcript_lines)
        prompt = f"{_EXTRACT_FACTS_PROMPT}\n\nConversation:\n{transcript}"

        llm = get_llm()
        response = await llm.ainvoke(prompt)
        raw = response.content if isinstance(response.content, str) else str(response.content)

        # Parse JSON from the response (handle markdown fences).
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        facts = [str(f)[:500] for f in data.get("facts", [])[:5] if f]
        decision = _parse_decision(data.get("decision"))

        return facts, decision

    except Exception:
        logger.debug("Fact/decision extraction failed", exc_info=True)
        return [], None


async def _extract_facts(messages: list[Any]) -> list[str]:
    """Backward-compatible facts-only accessor. Delegates to the combined extractor."""
    facts, _ = await _extract_facts_and_decision(messages)
    return facts


async def extract_and_store_memories(
    org_id: str,
    user_id: str,
    messages: list[Any],
    run_id: str,
    tool_names_used: list[str] | None = None,
    slots: dict[str, Any] | None = None,
    outcome: int = 0,
    outcome_source: str = "",
) -> int:
    """Extract facts and a decision summary from a conversation; store both.

    Facts are stored as individual ``org_memories`` rows (unchanged contract:
    returns the number of memories successfully stored). The decision summary
    is additionally stored once as a ``run_decisions`` row when present — the
    foundation for outcome-linked tool reputation. Both writes are
    independently best-effort. Fire-and-forget safe — never raises.
    """
    try:
        if tool_names_used and _is_stateless_lookup_run(messages, tool_names_used):
            logger.debug("Skipping memory extraction for stateless lookup run org_id=%s run_id=%s", org_id, run_id)
            return 0

        facts, decision = await _extract_facts_and_decision(messages)
        entries = await asyncio.gather(*[store_memory(org_id, user_id, fact, source_run_id=run_id) for fact in facts])
        stored = sum(1 for entry in entries if entry is not None)
        if stored > 0:
            logger.info("Stored %d memories for org_id=%s run_id=%s", stored, org_id, run_id)

        if decision is not None:
            await store_run_decision(
                org_id=org_id,
                user_id=user_id,
                run_id=run_id,
                decision=decision,
                tool_names=tool_names_used,
                slots=slots,
                outcome=outcome,
                outcome_source=outcome_source,
            )

        return stored
    except Exception:
        logger.exception("extract_and_store_memories failed for run_id=%s", run_id)
        return 0
