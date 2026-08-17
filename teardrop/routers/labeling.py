# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped API for generalized prediction labeling."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from labeling.contracts import ScoreResult
from labeling.store import (
    append_result_override,
    create_binding,
    get_definition,
    list_definitions,
    list_predictions,
    list_results,
)
from scheduling import get_scheduled_run
from teardrop.dependencies import _require_org_id, require_auth

router = APIRouter()


class LabelingDefinitionItem(BaseModel):
    definition_key: str
    definition_version: int
    prediction_schema: dict[str, Any]
    target_schema: dict[str, Any]
    outcome_schema: dict[str, Any]
    active: bool
    created_at: str


class LabelingDefinitionListResponse(BaseModel):
    items: list[LabelingDefinitionItem]


class LabelingBindingRequest(BaseModel):
    schedule_id: str = Field(..., min_length=1, max_length=256)
    definition_key: str = Field(..., min_length=1, max_length=128)
    definition_version: int = Field(..., gt=0)


class LabelingBindingResponse(BaseModel):
    id: str
    schedule_id: str
    definition_key: str
    definition_version: int
    status: Literal["created"]


class LabelingPredictionItem(BaseModel):
    id: str
    source_kind: str
    source_id: str
    run_id: str
    schedule_id: str
    definition_key: str
    definition_version: int
    predictions: dict[str, Any]
    payload_sha256: str
    prediction_at: str
    status: str
    parse_error: str
    created_at: str


class LabelingPredictionListResponse(BaseModel):
    items: list[LabelingPredictionItem]


class LabelingResultItem(BaseModel):
    id: str
    target_id: str
    scorer_key: str
    scorer_version: str
    observation_id: str | None
    actual: dict[str, Any] | None
    label: str
    score: float | None
    status: str
    source: str
    rationale: str
    created_at: str


class LabelingResultListResponse(BaseModel):
    items: list[LabelingResultItem]


class LabelingOverrideResponse(BaseModel):
    status: Literal["recorded"]


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@router.get("/labeling/definitions", tags=["Labeling"], response_model=LabelingDefinitionListResponse)
async def get_labeling_definitions(payload: dict = Depends(require_auth)) -> JSONResponse:
    _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    rows = await list_definitions()
    return JSONResponse(
        content={
            "items": [
                {
                    **row,
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ]
        }
    )


@router.post(
    "/labeling/bindings",
    tags=["Labeling"],
    response_model=LabelingBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bind_labeling_definition(
    body: LabelingBindingRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    org_id = _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    schedule = await get_scheduled_run(body.schedule_id, org_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled run not found.")
    definition = await get_definition(body.definition_key, body.definition_version)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Labeling definition not found.")
    binding_id = await create_binding(
        org_id=org_id,
        source_kind="scheduled_run",
        source_id=body.schedule_id,
        definition_key=definition.key,
        definition_version=definition.version,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": binding_id,
            "schedule_id": body.schedule_id,
            "definition_key": definition.key,
            "definition_version": definition.version,
            "status": "created",
        },
    )


@router.get("/labeling/predictions", tags=["Labeling"], response_model=LabelingPredictionListResponse)
async def get_labeling_predictions(
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    org_id = _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    rows = await list_predictions(org_id, limit)
    return JSONResponse(
        content={
            "items": [
                {
                    **row,
                    "prediction_at": _iso(row["prediction_at"]),
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ]
        }
    )


@router.get("/labeling/results", tags=["Labeling"], response_model=LabelingResultListResponse)
async def get_labeling_results(
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    org_id = _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    rows = await list_results(org_id, limit)
    return JSONResponse(
        content={
            "items": [
                {
                    **row,
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ]
        }
    )


@router.post(
    "/labeling/results/{target_id}/override",
    tags=["Labeling"],
    response_model=LabelingOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
async def override_labeling_result(
    target_id: str,
    body: ScoreResult,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    org_id = _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    if body.source == "automatic":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Override source is invalid.")
    recorded = await append_result_override(target_id=target_id, org_id=org_id, result=body)
    if not recorded:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found or currently leased.")
    return JSONResponse(status_code=status.HTTP_201_CREATED, content={"status": "recorded"})
