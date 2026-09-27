# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Agent decision and run-outcome routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import get_invoice_by_run
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.memory import backfill_decision_outcome, list_run_decisions

router = APIRouter()


class RunOutcomeRequest(BaseModel):
    rating: int = Field(..., ge=-1, le=1, description="-1 (bad outcome), 0 (neutral), or 1 (good outcome)")


class AgentDecisionRecord(BaseModel):
    id: str = Field(..., description="Decision record ID (UUID string).")
    run_id: str = Field(..., description="Run this decision summarizes.")
    task_class: str = Field(default="", description="Auto-classified task type; empty string if unclassified.")
    action: str = Field(default="", description="Action the planner took.")
    reasoning: str = Field(default="", description="Planner's stated reasoning for the action.")
    confidence: float | None = Field(default=None, description="Planner confidence score, if recorded.")
    tool_names: list[str] = Field(default_factory=list, description="Tools used while making this decision.")
    outcome: int = Field(..., ge=-1, le=1, description="-1 (bad), 0 (neutral/unlabeled), or 1 (good).")
    outcome_source: str = Field(default="", description="Origin of the outcome label (e.g. 'feedback'); empty if unlabeled.")
    created_at: str = Field(..., description="ISO 8601 creation timestamp.")


class AgentDecisionListResponse(BaseModel):
    items: list[AgentDecisionRecord]
    next_cursor: str | None = Field(
        default=None, description="ISO datetime cursor for the next page; null when no more items remain."
    )


@router.get("/agent/decisions", tags=["Agent"], response_model=AgentDecisionListResponse)
async def list_agent_decisions(
    payload: dict = Depends(require_auth),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="ISO datetime cursor for pagination"),
) -> JSONResponse:
    """List stored decision records for the authenticated org (newest first, cursor-paginated).

    Each record summarizes one agent run: the action taken, reasoning, task
    classification, tools used, and — once labeled — an outcome rating. This
    is the decision graph read surface; it is populated asynchronously after
    ``POST /agent/run`` completes and may lag briefly behind the SSE stream.
    """
    org_id = _require_org_id(payload, "No org_id in token — decisions require an org-scoped credential.")

    from shared.pagination import parse_cursor  # noqa: PLC0415

    cursor_dt = parse_cursor(cursor)
    rows = await list_run_decisions(org_id, limit, cursor_dt)
    serialized = [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "task_class": r["task_class"],
            "action": r["action"],
            "reasoning": r["reasoning"],
            "confidence": r["confidence"],
            "tool_names": r["tool_names"],
            "outcome": r["outcome"],
            "outcome_source": r["outcome_source"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


class RunOutcomeResponse(BaseModel):
    status: Literal["recorded"]


@router.patch("/agent/runs/{run_id}/outcome", tags=["Agent"], response_model=RunOutcomeResponse)
async def set_agent_run_outcome(
    run_id: str,
    body: RunOutcomeRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Label the ground-truth outcome (-1/0/1) of a past run — feeds the decision graph.

    Ownership is verified the same way as marketplace tool feedback
    (``submit_marketplace_tool_feedback``): the run must belong to the
    authenticated user's own invoice history. The label is applied once —
    resubmitting after a label already exists returns 404 rather than
    silently overwriting it.
    """
    org_id = _require_org_id(payload, "No org_id in token — outcomes require an org-scoped credential.")
    user_id = payload.get("sub", "")

    invoice = await get_invoice_by_run(run_id, user_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found for this account.")

    updated = await backfill_decision_outcome(run_id, org_id, body.rating, source="explicit")
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No decision record found for this run, or its outcome was already set.",
        )
    return JSONResponse(content={"status": "recorded"})
