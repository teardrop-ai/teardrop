# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Persistent agent memory data layer (async Postgres + pgvector).

Provides:
- MemoryEntry model
- init_memory_db()              — register pgvector types on startup
- store_memory()                — embed + INSERT (fire-and-forget safe)
- recall_memories()             — cosine-similarity search over org memories
- extract_and_store_memories()  — LLM-based fact extraction + batch store
- list_memories()               — cursor-paginated listing
- delete_memory()               — org-scoped single delete
- delete_all_org_memories()     — admin purge
- count_memories()              — count per org
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from config import get_settings

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


# ─── Fact extraction (LLM-based) ─────────────────────────────────────────────

_EXTRACT_FACTS_PROMPT = """\
You are a factual-memory extractor. Given a conversation between a user and an \
AI assistant, extract up to 5 key factual statements worth remembering for \
future interactions. Focus on:
- User preferences, constraints, or recurring requests
- Domain-specific facts the user shared (wallet addresses, project names, etc.)
- Decisions made or conclusions reached

Rules:
- Each fact must be a single, self-contained sentence (max 500 characters).
- Do NOT include greetings, pleasantries, or meta-commentary.
- If the conversation contains no memorable facts, return an empty list.

Respond with ONLY a JSON object:
{"facts": ["fact one", "fact two", ...]}
"""


async def _extract_facts(messages: list[Any]) -> list[str]:
    """Use the configured LLM to extract key facts from a conversation.

    Returns a list of fact strings (max 5). Returns empty list on any error.
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
            return []

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
        facts = data.get("facts", [])

        # Enforce limits.
        return [str(f)[:500] for f in facts[:5] if f]

    except Exception:
        logger.debug("Fact extraction failed", exc_info=True)
        return []


async def extract_and_store_memories(
    org_id: str,
    user_id: str,
    messages: list[Any],
    run_id: str,
) -> int:
    """Extract facts from a conversation and store each as a memory.

    Returns the number of memories successfully stored.
    Fire-and-forget safe — never raises.
    """
    try:
        facts = await _extract_facts(messages)
        stored = 0
        for fact in facts:
            entry = await store_memory(org_id, user_id, fact, source_run_id=run_id)
            if entry is not None:
                stored += 1
        if stored > 0:
            logger.info("Stored %d memories for org_id=%s run_id=%s", stored, org_id, run_id)
        return stored
    except Exception:
        logger.exception("extract_and_store_memories failed for run_id=%s", run_id)
        return 0
