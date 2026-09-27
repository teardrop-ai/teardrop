# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Public, unauthenticated scorecards for pre-registered verified-outcome tasks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from labeling.contracts import Definition
from labeling.scorecards import compute_scorecards, definition_sha256, get_public_definition, list_public_definitions
from shared.request_ip import client_ip_from_request
from teardrop.config import get_settings
from teardrop.rate_limit import _enforce_rate_limit

router = APIRouter()

_DEFINITION_KEY = r"^[a-z0-9][a-z0-9_.-]{0,127}$"
_SUBJECT = r"^(0x[0-9a-f]{40}|schedule:[A-Za-z0-9_-]{1,128})$"
_WINDOWS = (30, 90, 365)
_CACHE_HEADERS = {"Cache-Control": "public, max-age=300"}


class ScorecardTask(BaseModel):
    definition_key: str
    definition_version: int
    definition_sha256: str
    prediction_schema: dict[str, Any]
    config: dict[str, Any]


class ScorecardTaskListResponse(BaseModel):
    items: list[ScorecardTask]


class CalibrationBin(BaseModel):
    bin: int
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_frequency: float


class ScorecardItem(BaseModel):
    subject: str
    platform_attested: bool
    eligible: bool
    n_scored: int
    rounds_submitted: int
    rounds_expected: int
    coverage: float | None
    unresolved: int
    mean_brier: float | None
    adjusted_brier: float | None
    accuracy: float | None
    calibration: list[CalibrationBin] | None = None


class LeaderboardResponse(BaseModel):
    definition_key: str
    definition_version: int
    definition_sha256: str
    window_days: int
    min_sample: int
    items: list[ScorecardItem]


class ScorecardResponse(BaseModel):
    definition_key: str
    definition_version: int
    definition_sha256: str
    window_days: int
    min_sample: int
    item: ScorecardItem


def _task(definition: Definition) -> dict[str, Any]:
    return {
        "definition_key": definition.key,
        "definition_version": definition.version,
        "definition_sha256": definition_sha256(definition),
        "prediction_schema": definition.prediction_schema,
        "config": definition.config,
    }


async def _admit(request: Request) -> None:
    settings = get_settings()
    if not settings.vor_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scorecards are disabled.")
    client_ip = client_ip_from_request(request, trusted_proxy_count=settings.trusted_proxy_count)
    await _enforce_rate_limit(f"scorecards:{client_ip}", settings.rate_limit_auth_rpm)


async def _public_definition(definition_key: str, definition_version: int) -> Definition:
    definition = await get_public_definition(definition_key, definition_version)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Public task not found.")
    return definition


def _window(window_days: int) -> int:
    if window_days not in _WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="window_days must be one of 30, 90, or 365.",
        )
    return window_days


def _header(definition: Definition, window_days: int) -> dict[str, Any]:
    return {
        "definition_key": definition.key,
        "definition_version": definition.version,
        "definition_sha256": definition_sha256(definition),
        "window_days": window_days,
        "min_sample": max(1, int(definition.config.get("min_sample", 1))),
    }


@router.get("/scorecards/tasks", tags=["Scorecards"], response_model=ScorecardTaskListResponse)
async def list_scorecard_tasks(request: Request) -> JSONResponse:
    """List pre-registered public tasks with a hash of each immutable definition."""
    await _admit(request)
    definitions = await list_public_definitions()
    return JSONResponse(content={"items": [_task(item) for item in definitions]}, headers=_CACHE_HEADERS)


@router.get(
    "/scorecards/{definition_key}/{definition_version}",
    tags=["Scorecards"],
    response_model=LeaderboardResponse,
)
async def get_scorecard_leaderboard(
    request: Request,
    definition_key: str = Path(..., pattern=_DEFINITION_KEY),
    definition_version: int = Path(..., gt=0, le=1_000_000),
    window_days: int = Query(default=90),
) -> JSONResponse:
    """Rank subjects meeting the task's minimum sample by coverage-adjusted Brier score (lower is better)."""
    await _admit(request)
    window = _window(window_days)
    definition = await _public_definition(definition_key, definition_version)
    cards = await compute_scorecards(definition, window)
    return JSONResponse(
        content={**_header(definition, window), "items": [card for card in cards if card["eligible"]]},
        headers=_CACHE_HEADERS,
    )


@router.get(
    "/scorecards/{definition_key}/{definition_version}/{subject}",
    tags=["Scorecards"],
    response_model=ScorecardResponse,
)
async def get_subject_scorecard(
    request: Request,
    definition_key: str = Path(..., pattern=_DEFINITION_KEY),
    definition_version: int = Path(..., gt=0, le=1_000_000),
    subject: str = Path(..., pattern=_SUBJECT),
    window_days: int = Query(default=90),
) -> JSONResponse:
    """One subject's scorecard; metrics and calibration are withheld below the minimum sample."""
    await _admit(request)
    window = _window(window_days)
    definition = await _public_definition(definition_key, definition_version)
    cards = await compute_scorecards(definition, window, subject)
    if not cards:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scorecard not found.")
    return JSONResponse(content={**_header(definition, window), "item": cards[0]}, headers=_CACHE_HEADERS)
