# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Org-scoped API for generalized prediction labeling."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from labeling.anchor import batch_leaves
from labeling.commitments import (
    HASH_ALGORITHM,
    LEAF_VERSION_PREDICTION,
    audit_path,
    leaf_hash,
    merkle_root,
    prediction_leaf_fields,
    prediction_signing_message,
    recover_signer,
)
from labeling.contracts import ScoreResult, validate_prediction
from labeling.registry import resolve_parser
from labeling.store import (
    PredictionConflictError,
    append_result_override,
    create_binding,
    get_definition,
    get_prediction_commitment,
    insert_prediction,
    list_definitions,
    list_predictions,
    list_results,
)
from scheduling import get_scheduled_run
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.rate_limit import _enforce_rate_limit
from teardrop.wallets import is_wallet_linked_to_org

logger = logging.getLogger(__name__)

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


class PredictionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    definition_version: int = Field(..., gt=0)
    idempotency_key: str = Field(..., pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    signer_address: str = Field(..., pattern=r"^0x[0-9a-fA-F]{40}$")
    signature: str = Field(..., pattern=r"^0x[0-9a-fA-F]{130}$")
    predictions: dict[str, Any]


class PredictionSubmitResponse(BaseModel):
    id: str
    payload_sha256: str
    status: Literal["accepted"]
    created: bool


class PredictionProofAnchor(BaseModel):
    batch_id: str
    leaf_index: int
    tree_size: int
    merkle_root: str
    audit_path: list[str]
    chain_id: int
    tx_hash: str | None
    anchor_address: str | None
    block_number: int | None
    anchored_at: str | None


class PredictionProofResponse(BaseModel):
    prediction_id: str
    status: Literal["pending", "submitted", "anchored"]
    hash_algorithm: Literal["rfc6962-sha256"]
    leaf_version: int
    leaf_preimage: dict[str, Any]
    salt: str
    leaf_sha256: str
    anchor: PredictionProofAnchor | None


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


@router.post(
    "/labeling/predictions",
    tags=["Labeling"],
    response_model=PredictionSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Idempotent replay of an existing submission."},
        401: {"description": "Signature does not match the canonical payload."},
        403: {"description": "Signer wallet is not linked to the organization."},
        409: {"description": "Idempotency key was reused with a different payload."},
    },
)
async def submit_labeling_prediction(
    body: PredictionSubmitRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Commit a signed external prediction; the server assigns its timestamp."""
    settings = get_settings()
    if not settings.vor_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="External prediction submission is disabled.")
    org_id = _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    await _enforce_rate_limit(
        f"vor:submit:{org_id}",
        settings.rate_limit_vor_submit_rpm,
        detail="Rate limit exceeded for prediction submission.",
    )
    definition = await get_definition(body.definition_key, body.definition_version)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Labeling definition not found.")
    try:
        payload_sha256 = validate_prediction(body.predictions, definition.prediction_schema)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None

    signer = body.signer_address.lower()
    message = prediction_signing_message(
        org_id=org_id,
        definition_key=definition.key,
        definition_version=definition.version,
        idempotency_key=body.idempotency_key,
        payload_sha256=payload_sha256,
    )
    if recover_signer(message, body.signature) != signer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Signature does not match the canonical payload (payload_sha256={payload_sha256}).",
        )
    if not await is_wallet_linked_to_org(org_id, signer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signer wallet is not linked to this organization.")

    from labeling.adapters import register_builtin_adapters

    register_builtin_adapters()
    now = datetime.now(timezone.utc)
    try:
        targets = list(resolve_parser(definition.parser_key, definition.parser_version)(body.predictions, definition, now))
    except (LookupError, ValueError):
        targets = []
    if not targets:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Prediction could not be expanded into scoring targets.",
        )
    if min(target.window_end for target in targets) < now + timedelta(seconds=2 * settings.vor_anchor_interval_seconds):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Prediction horizon is too short to be anchored before its window closes.",
        )
    try:
        prediction_id, created = await insert_prediction(
            org_id=org_id,
            source_kind="external",
            source_id=body.idempotency_key,
            run_id="",
            schedule_id="",
            binding_id=None,
            definition=definition,
            predictions=body.predictions,
            targets=targets,
            prediction_at=now,
            signer_address=signer,
            signature=body.signature.lower(),
            commit=True,
        )
    except PredictionConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used with a different payload.",
        ) from None
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content={"id": prediction_id, "payload_sha256": payload_sha256, "status": "accepted", "created": created},
    )


@router.get(
    "/labeling/predictions/{prediction_id}/proof",
    tags=["Labeling"],
    response_model=PredictionProofResponse,
)
async def get_labeling_prediction_proof(
    prediction_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return the commitment leaf and, once sealed, its Merkle inclusion proof and anchor."""
    org_id = _require_org_id(payload, "No org_id in token - labeling requires an org-scoped credential.")
    row = await get_prediction_commitment(org_id, prediction_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Committed prediction not found.")
    preimage = prediction_leaf_fields(
        prediction_id=str(row["id"]),
        org_id=str(row["org_id"]),
        signer_address=row["signer_address"],
        definition_key=str(row["definition_key"]),
        definition_version=int(row["definition_version"]),
        payload_sha256=str(row["payload_sha256"]),
        prediction_at=row["prediction_at"],
    )
    leaf = str(row["leaf_sha256"])
    if leaf_hash(LEAF_VERSION_PREDICTION, preimage, str(row["commit_salt"])) != leaf:
        logger.error("commitment leaf integrity check failed prediction_id=%s", prediction_id)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Commitment integrity check failed.")

    anchor = None
    proof_status = "pending"
    if row["anchor_batch_id"] is not None:
        leaves = await batch_leaves(str(row["anchor_batch_id"]), int(row["leaf_count"]))
        if merkle_root(leaves) != row["merkle_root"]:
            logger.error("commitment batch integrity check failed batch_id=%s", row["anchor_batch_id"])
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Commitment integrity check failed.")
        index = int(row["anchor_leaf_index"])
        if row["block_number"] is not None:
            proof_status = "anchored"
        elif row["tx_hash"] is not None:
            proof_status = "submitted"
        anchor = {
            "batch_id": str(row["anchor_batch_id"]),
            "leaf_index": index,
            "tree_size": len(leaves),
            "merkle_root": str(row["merkle_root"]),
            "audit_path": audit_path(leaves, index),
            "chain_id": int(row["chain_id"]),
            "tx_hash": row["tx_hash"],
            "anchor_address": row["anchor_address"],
            "block_number": row["block_number"],
            "anchored_at": _iso(row["anchored_at"]) if row["anchored_at"] is not None else None,
        }
    return JSONResponse(
        content={
            "prediction_id": str(row["id"]),
            "status": proof_status,
            "hash_algorithm": HASH_ALGORITHM,
            "leaf_version": LEAF_VERSION_PREDICTION,
            "leaf_preimage": preimage,
            "salt": str(row["commit_salt"]),
            "leaf_sha256": leaf,
            "anchor": anchor,
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target not found, currently leased, or awaiting automatic scoring.",
        )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content={"status": "recorded"})
