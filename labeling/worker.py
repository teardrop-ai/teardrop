# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from labeling.contracts import Definition, ScoreResult, TargetDraft
from labeling.registry import resolve_provider, resolve_scorer
from labeling.store import claim_due_targets, complete_target, retry_target, store_observation

logger = logging.getLogger(__name__)


def _definition_from_row(row: Any) -> Definition:
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


def _target_from_row(row: Any) -> TargetDraft:
    return TargetDraft(
        item_key=str(row["item_key"]),
        item_payload=dict(row["item_payload"] or {}),
        window_start=row["window_start"],
        window_end=row["window_end"],
        due_at=row["window_end"],
    )


def _retry_delay(attempts: int) -> int:
    return min(86_400, 2 ** max(0, min(attempts - 1, 10)))


async def _retry_or_complete_unavailable(row: Any, error: str) -> bool:
    target_id = str(row["id"])
    lease_token = str(row["lease_token"])
    if int(row["attempts"]) >= 5:
        return await complete_target(
            target_id=target_id,
            lease_token=lease_token,
            result=ScoreResult(
                label="unavailable",
                status="unavailable",
                rationale="Labeling data was unavailable after bounded retries.",
            ),
            observation_id=None,
        )
    return await retry_target(target_id, lease_token, error, _retry_delay(int(row["attempts"])))


async def _process_claimed_rows(rows: list[Any]) -> int:
    grouped: dict[tuple[str, str, str, int], list[tuple[Any, Definition, TargetDraft, Any]]] = defaultdict(list)
    for row in rows:
        definition = _definition_from_row(row)
        target = _target_from_row(row)
        try:
            provider = resolve_provider(definition.provider_key, definition.provider_version)
            request = provider.plan(target, definition)
            grouped[(definition.provider_key, definition.provider_version, definition.key, definition.version)].append(
                (row, definition, target, request)
            )
        except Exception as exc:
            await _retry_or_complete_unavailable(row, "observation planning failed")
            logger.warning("labeling target planning failed target_id=%s error=%s", row["id"], type(exc).__name__)

    completed = 0
    for group in grouped.values():
        definition = group[0][1]
        provider = resolve_provider(definition.provider_key, definition.provider_version)
        try:
            observations = await provider.fetch_batch([item[3] for item in group], definition)
        except Exception:
            observations = {}
            logger.warning("labeling provider batch failed provider=%s", definition.provider_key, exc_info=True)
        for row, definition, target, request in group:
            observation = observations.get(request.request_sha256)
            try:
                if observation is None:
                    raise RuntimeError("observation unavailable")
                observation_id = await store_observation(observation)
                scorer = resolve_scorer(definition.scorer_key, definition.scorer_version)
                score = scorer(target.item_payload, observation, definition)
                if not isinstance(score, ScoreResult):
                    raise ValueError("scorer returned an invalid result")
                if await complete_target(
                    target_id=str(row["id"]),
                    lease_token=str(row["lease_token"]),
                    result=score,
                    observation_id=observation_id,
                ):
                    completed += 1
            except Exception as exc:
                target_id = str(row["id"])
                await _retry_or_complete_unavailable(row, "labeling work failed")
                logger.warning("labeling target processing failed target_id=%s error=%s", target_id, type(exc).__name__)
    return completed


async def labeling_tick(limit: int = 50, max_per_org: int = 10, lease_seconds: int = 120) -> int:
    from labeling.adapters import register_builtin_adapters

    register_builtin_adapters()
    rows = await claim_due_targets(limit, max_per_org, lease_seconds)
    if not rows:
        return 0
    return await _process_claimed_rows(rows)
