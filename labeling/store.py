# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import asyncpg

from labeling.contracts import Definition, Observation, ScoreResult, TargetDraft, canonical_json

_pool: asyncpg.Pool | None = None


async def init_labeling_db(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool


async def close_labeling_db() -> None:
    global _pool
    _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Labeling DB not initialised - call init_labeling_db() first")
    return _pool


def _definition_from_row(row: asyncpg.Record) -> Definition:
    return Definition(
        key=str(row["definition_key"]),
        version=int(row["definition_version"]),
        prediction_schema=dict(row["prediction_schema"] or {}),
        target_schema=dict(row["target_schema"] or {}),
        outcome_schema=dict(row["outcome_schema"] or {}),
        parser_key=str(row["parser_key"]),
        parser_version=str(row["parser_version"]),
        provider_key=str(row["provider_key"]),
        provider_version=str(row["provider_version"]),
        scorer_key=str(row["scorer_key"]),
        scorer_version=str(row["scorer_version"]),
        config=dict(row["config"] or {}),
    )


async def get_definition(key: str, version: int, *, active_only: bool = True) -> Definition | None:
    active_clause = "AND active = TRUE" if active_only else ""
    row = await _get_pool().fetchrow(
        f"""
        SELECT definition_key, definition_version, prediction_schema, target_schema,
               outcome_schema, parser_key, parser_version, provider_key, provider_version,
               scorer_key, scorer_version, config
        FROM labeling_definitions
        WHERE definition_key = $1 AND definition_version = $2 {active_clause}
        """,
        key,
        version,
    )
    return _definition_from_row(row) if row is not None else None


async def get_active_definition(key: str) -> Definition | None:
    row = await _get_pool().fetchrow(
        """
        SELECT definition_key, definition_version, prediction_schema, target_schema,
               outcome_schema, parser_key, parser_version, provider_key, provider_version,
               scorer_key, scorer_version, config
        FROM labeling_definitions
        WHERE definition_key = $1 AND active = TRUE
        ORDER BY definition_version DESC
        LIMIT 1
        """,
        key,
    )
    return _definition_from_row(row) if row is not None else None


async def list_definitions() -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(
        """
        SELECT definition_key, definition_version, prediction_schema,
               target_schema, outcome_schema, active, created_at
        FROM labeling_definitions
        WHERE active = TRUE
        ORDER BY definition_key, definition_version DESC
        """
    )
    return [dict(row) for row in rows]


async def create_binding(
    *,
    org_id: str,
    source_kind: str,
    source_id: str,
    definition_key: str,
    definition_version: int,
) -> str:
    binding_id = str(uuid.uuid4())
    await _get_pool().execute(
        """
        INSERT INTO labeling_bindings
            (id, org_id, source_kind, source_id, definition_key, definition_version)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (org_id, source_kind, source_id) DO UPDATE
        SET org_id = EXCLUDED.org_id,
            definition_key = EXCLUDED.definition_key,
            definition_version = EXCLUDED.definition_version,
            enabled = TRUE
        """,
        binding_id,
        org_id,
        source_kind,
        source_id,
        definition_key,
        definition_version,
    )
    row = await _get_pool().fetchrow(
        "SELECT id FROM labeling_bindings WHERE org_id = $1 AND source_kind = $2 AND source_id = $3",
        org_id,
        source_kind,
        source_id,
    )
    if row is None:
        raise RuntimeError("Labeling binding was not persisted")
    return str(row["id"])


async def get_binding_for_schedule(schedule_id: str, org_id: str) -> asyncpg.Record | None:
    return await _get_pool().fetchrow(
        """
        SELECT b.id, b.org_id, b.source_kind, b.source_id, b.definition_key,
               b.definition_version
        FROM labeling_bindings b
                WHERE b.source_kind = 'scheduled_run'
                    AND b.source_id = $1
                    AND b.org_id = $2
          AND b.enabled = TRUE
        """,
        schedule_id,
        org_id,
    )


async def insert_prediction(
    *,
    org_id: str,
    source_kind: str,
    source_id: str,
    run_id: str,
    schedule_id: str,
    binding_id: str | None,
    definition: Definition,
    predictions: dict[str, Any],
    targets: Iterable[TargetDraft],
    prediction_at: datetime | None = None,
    parse_error: str = "",
) -> tuple[str, bool]:
    encoded = canonical_json(predictions)
    payload_hash = __import__("hashlib").sha256(encoded.encode("utf-8")).hexdigest()
    prediction_id = str(uuid.uuid4())
    status = "invalid" if parse_error else "accepted"
    created_at = prediction_at or datetime.now(timezone.utc)
    target_rows = list(targets)
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO labeling_predictions
                    (id, org_id, source_kind, source_id, run_id, schedule_id, binding_id,
                     definition_key, definition_version, predictions, payload_sha256,
                     prediction_at, status, parse_error, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $12)
                ON CONFLICT (org_id, source_kind, source_id, definition_key, definition_version)
                DO NOTHING
                RETURNING id
                """,
                prediction_id,
                org_id,
                source_kind,
                source_id,
                run_id,
                schedule_id,
                binding_id,
                definition.key,
                definition.version,
                encoded,
                payload_hash,
                created_at,
                status,
                parse_error[:2000],
            )
            inserted = row is not None
            persisted_status = status
            if not inserted:
                row = await conn.fetchrow(
                    """
                    SELECT id, status
                    FROM labeling_predictions
                                        WHERE org_id = $1 AND source_kind = $2 AND source_id = $3
                                            AND definition_key = $4 AND definition_version = $5
                    """,
                    org_id,
                    source_kind,
                    source_id,
                    definition.key,
                    definition.version,
                )
            if row is None:
                raise RuntimeError("Labeling prediction was not persisted")
            prediction_id = str(row["id"])
            if not inserted:
                persisted_status = str(row["status"])
            if persisted_status == "accepted":
                await conn.executemany(
                    """
                    INSERT INTO labeling_targets
                        (id, prediction_id, org_id, item_key, item_payload,
                         window_start, window_end, due_at)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                    ON CONFLICT (prediction_id, item_key) DO NOTHING
                    """,
                    [
                        (
                            str(uuid.uuid4()),
                            prediction_id,
                            org_id,
                            target.item_key,
                            canonical_json(target.item_payload),
                            target.window_start,
                            target.window_end,
                            target.due_at,
                        )
                        for target in target_rows
                    ],
                )
    return prediction_id, inserted


async def claim_due_targets(limit: int, max_per_org: int, lease_seconds: int) -> list[asyncpg.Record]:
    if limit <= 0 or max_per_org <= 0 or lease_seconds <= 0:
        return []
    token = str(uuid.uuid4())
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY due_at ASC, id ASC) AS org_rank
                    FROM labeling_targets
                    WHERE due_at <= NOW()
                      AND (
                          status = 'pending'
                          OR (status = 'leased' AND lease_expires_at <= NOW())
                      )
                ), due AS (
                    SELECT target.id
                    FROM labeling_targets target
                    JOIN ranked ON ranked.id = target.id
                    WHERE ranked.org_rank <= $2
                    ORDER BY target.due_at ASC, target.id ASC
                    LIMIT $1
                    FOR UPDATE OF target SKIP LOCKED
                )
                UPDATE labeling_targets target
                SET status = 'leased',
                    attempts = target.attempts + 1,
                    lease_token = $3,
                    lease_expires_at = NOW() + ($4 * INTERVAL '1 second'),
                    last_error = ''
                FROM due
                WHERE target.id = due.id
                RETURNING target.id, target.prediction_id, target.org_id,
                          target.item_key, target.item_payload, target.window_start,
                          target.window_end, target.attempts, target.lease_token,
                          p.definition_key, p.definition_version,
                          d.prediction_schema, d.target_schema, d.outcome_schema,
                          d.parser_key, d.parser_version, d.provider_key,
                          d.provider_version, d.scorer_key, d.scorer_version, d.config
                """,
                limit,
                max_per_org,
                token,
                lease_seconds,
            )
    return list(rows)


async def store_observation(observation: Observation) -> str:
    observation_id = str(uuid.uuid4())
    request = observation.request
    encoded = canonical_json(observation.payload) if observation.payload is not None else None
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO labeling_observations
                (id, scope_key, provider_key, provider_version, request_sha256, as_of,
                 payload, status, error)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            ON CONFLICT (scope_key, provider_key, provider_version, request_sha256)
            DO NOTHING
            RETURNING id
            """,
            observation_id,
            request.scope_key,
            request.provider_key,
            request.provider_version,
            request.request_sha256,
            request.as_of,
            encoded,
            observation.status,
            observation.error[:2000],
        )
        if row is not None:
            return str(row["id"])
        existing = await conn.fetchrow(
            """
            SELECT id FROM labeling_observations
            WHERE scope_key = $1 AND provider_key = $2 AND provider_version = $3 AND request_sha256 = $4
            """,
            request.scope_key,
            request.provider_key,
            request.provider_version,
            request.request_sha256,
        )
    if existing is None:
        raise RuntimeError("Labeling observation was not persisted")
    return str(existing["id"])


async def complete_target(
    *,
    target_id: str,
    lease_token: str,
    result: ScoreResult,
    observation_id: str | None,
) -> bool:
    result_id = str(uuid.uuid4())
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                """
                UPDATE labeling_targets
                SET status = CASE
                        WHEN $2 IN ('correct', 'incorrect', 'neutral', 'inconclusive') THEN 'scored'
                        WHEN $2 = 'unavailable' THEN 'unavailable'
                        ELSE 'invalid'
                    END,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = CASE WHEN $2 IN ('unavailable', 'invalid') THEN $3 ELSE '' END
                WHERE id = $1 AND status = 'leased' AND lease_token = $4
                RETURNING id
                """,
                target_id,
                result.status,
                result.rationale[:2000],
                lease_token,
            )
            if target is None:
                return False
            await conn.execute(
                """
                INSERT INTO labeling_results
                    (id, target_id, scorer_key, scorer_version, observation_id,
                     actual, label, score, status, source, rationale)
                SELECT $1, t.id, d.scorer_key, d.scorer_version, $2,
                       $3::jsonb, $4, $5, $6, $7, $8
                FROM labeling_targets t
                JOIN labeling_predictions p ON p.id = t.prediction_id
                JOIN labeling_definitions d
                  ON d.definition_key = p.definition_key
                 AND d.definition_version = p.definition_version
                WHERE t.id = $9
                """,
                result_id,
                observation_id,
                canonical_json(result.actual) if result.actual is not None else None,
                result.label,
                result.score,
                result.status,
                result.source,
                result.rationale[:2000],
                target_id,
            )
    return True


async def retry_target(target_id: str, lease_token: str, error: str, delay_seconds: int) -> bool:
    delay = max(1, min(delay_seconds, 86_400))
    result = await _get_pool().execute(
        """
        UPDATE labeling_targets
        SET status = 'pending',
            due_at = NOW() + ($3 * INTERVAL '1 second'),
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error = $4
        WHERE id = $1 AND status = 'leased' AND lease_token = $2
        """,
        target_id,
        lease_token,
        delay,
        error[:2000],
    )
    return result == "UPDATE 1"


async def append_result_override(
    *,
    target_id: str,
    org_id: str,
    result: ScoreResult,
) -> bool:
    result_id = str(uuid.uuid4())
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.id, t.status, d.scorer_key, d.scorer_version
                FROM labeling_targets t
                JOIN labeling_predictions p ON p.id = t.prediction_id
                JOIN labeling_definitions d
                  ON d.definition_key = p.definition_key
                 AND d.definition_version = p.definition_version
                WHERE t.id = $1 AND t.org_id = $2
                FOR UPDATE
                """,
                target_id,
                org_id,
            )
            if row is None or row["status"] == "leased":
                return False
            await conn.execute(
                """
                UPDATE labeling_targets
                SET status = CASE
                        WHEN $2 IN ('correct', 'incorrect', 'neutral', 'inconclusive') THEN 'scored'
                        WHEN $2 = 'unavailable' THEN 'unavailable'
                        ELSE 'invalid'
                    END,
                    last_error = ''
                WHERE id = $1 AND org_id = $3
                """,
                target_id,
                result.status,
                org_id,
            )
            await conn.execute(
                """
                INSERT INTO labeling_results
                    (id, target_id, scorer_key, scorer_version, actual, label,
                     score, status, source, rationale)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10)
                """,
                result_id,
                target_id,
                row["scorer_key"],
                row["scorer_version"],
                canonical_json(result.actual) if result.actual is not None else None,
                result.label,
                result.score,
                result.status,
                result.source,
                result.rationale[:2000],
            )
    return True


async def list_predictions(org_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(
        """
        SELECT id, org_id, source_kind, source_id, run_id, schedule_id,
               definition_key, definition_version, predictions, payload_sha256,
               prediction_at, status, parse_error, created_at
        FROM labeling_predictions
        WHERE org_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT $2
        """,
        org_id,
        max(1, min(limit, 100)),
    )
    return [dict(row) for row in rows]


async def list_results(org_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await _get_pool().fetch(
        """
        SELECT r.id, r.target_id, r.scorer_key, r.scorer_version, r.observation_id,
               r.actual, r.label, r.score, r.status, r.source, r.rationale, r.created_at
        FROM labeling_results r
        JOIN labeling_targets t ON t.id = r.target_id
        WHERE t.org_id = $1
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT $2
        """,
        org_id,
        max(1, min(limit, 100)),
    )
    return [dict(row) for row in rows]
