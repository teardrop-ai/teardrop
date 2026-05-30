# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Usage reporting, pricing, invoices, credit history, and top-up (Stripe + USDC) routes."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from billing import (
    build_usdc_topup_requirements,
    create_stripe_embedded_session,
    credit_usdc_topup,
    get_billing_history,
    get_credit_history,
    get_current_pricing,
    get_invoice_by_run,
    get_invoices,
    get_org_spending_config,
    get_stripe_session_status,
    get_tool_pricing_overrides,
    handle_stripe_webhook,
    verify_and_settle_usdc_topup,
)
from teardrop import rate_limit as _rate_limit
from teardrop.config import get_settings
from teardrop.dependencies import _require_org_id, require_auth
from teardrop.usage import get_usage_by_user

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


# ─── Usage endpoints ─────────────────────────────────────────────────────────


@router.get("/usage/me", tags=["Usage"])
async def usage_me(
    payload: dict = Depends(require_auth),
    start: str | None = None,
    end: str | None = None,
) -> JSONResponse:
    """Return aggregated usage for the authenticated user."""

    start_dt = datetime.fromisoformat(start) if start else None
    end_dt = datetime.fromisoformat(end) if end else None
    summary = await get_usage_by_user(payload["sub"], start_dt, end_dt)
    return JSONResponse(content=summary.model_dump())


# ─── Billing endpoints ───────────────────────────────────────────────────────


@router.get("/billing/pricing", tags=["Billing"])
async def billing_pricing() -> JSONResponse:
    """Return current pricing rules (public)."""
    if not settings.billing_enabled:
        return JSONResponse(
            content={"billing_enabled": False},
            headers={"Cache-Control": "public, max-age=60"},
        )
    pricing = await get_current_pricing()
    if pricing is None:
        return JSONResponse(
            content={"billing_enabled": True, "pricing": None},
            headers={"Cache-Control": "public, max-age=60"},
        )
    tool_overrides = await get_tool_pricing_overrides()
    pricing_data = pricing.model_dump(mode="json")
    pricing_data["tool_overrides"] = tool_overrides
    return JSONResponse(
        content={
            "billing_enabled": True,
            "pricing": pricing_data,
            "network": settings.x402_network,
        },
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/billing/history", tags=["Billing"])
async def billing_history(
    payload: dict = Depends(require_auth),
    limit: int = 50,
) -> JSONResponse:
    """Return settlement history for the authenticated user."""
    history = await get_billing_history(payload["sub"], min(limit, 200))
    return JSONResponse(content=[{**row, "created_at": row["created_at"].isoformat()} for row in history])


@router.get("/billing/balance", tags=["Billing"])
async def billing_balance(
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return the authenticated org's current credit balance."""
    org_id = _require_org_id(payload, "No org_id in token — credit balance requires an org-scoped credential.")
    spending = await get_org_spending_config(org_id)
    return JSONResponse(
        content={
            "org_id": org_id,
            "balance_usdc": spending["balance_usdc"],
            "spending_limit_usdc": spending["spending_limit_usdc"],
            "spending_limit_active": spending["spending_limit_usdc"] > 0,
            "is_paused": spending["is_paused"],
            "daily_spend_usdc": spending["daily_spend_usdc"],
        }
    )


@router.get("/billing/invoices", tags=["Billing"])
async def billing_invoices(
    payload: dict = Depends(require_auth),
    limit: int = 50,
    cursor: str | None = None,
) -> JSONResponse:
    """Return per-run invoice records for the authenticated user (cursor paginated)."""
    from shared.pagination import parse_cursor

    cursor_dt = parse_cursor(cursor)
    invoices = await get_invoices(payload["sub"], min(limit, 200), cursor_dt)
    serialized = [{**row, "created_at": row["created_at"].isoformat()} for row in invoices]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


@router.get("/billing/invoice/{run_id}", tags=["Billing"])
async def billing_invoice_by_run(
    run_id: str,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return a single run receipt scoped to the authenticated user."""
    invoice = await get_invoice_by_run(run_id, payload["sub"])
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return JSONResponse(content={**invoice, "created_at": invoice["created_at"].isoformat()})


@router.get("/billing/credit-history", tags=["Billing"])
async def billing_credit_history(
    payload: dict = Depends(require_auth),
    operation: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> JSONResponse:
    """Return credit ledger entries for the authenticated org (cursor paginated)."""
    from shared.pagination import parse_cursor

    org_id = _require_org_id(payload, "No org_id in token — credit history requires an org-scoped credential.")
    if operation is not None and operation not in ("debit", "topup"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="operation must be 'debit' or 'topup'",
        )
    cursor_dt = parse_cursor(cursor)
    entries = await get_credit_history(org_id, operation, min(limit, 200), cursor_dt)
    serialized = [{**row, "created_at": row["created_at"].isoformat()} for row in entries]
    next_cursor = serialized[-1]["created_at"] if serialized else None
    return JSONResponse(content={"items": serialized, "next_cursor": next_cursor})


# ─── Stripe top-up endpoints ─────────────────────────────────────────────────


class StripeTopupRequest(BaseModel):
    amount_cents: int = Field(..., ge=100, le=1_000_000, description="USD cents (100 = $1.00, max $10,000)")
    return_url: str = Field(
        ...,
        min_length=20,
        max_length=500,
        description="HTTPS return URL with {CHECKOUT_SESSION_ID} template",
    )


@router.post("/billing/topup/stripe", tags=["Billing"])
async def billing_topup_stripe(
    body: StripeTopupRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Create a Stripe Checkout session for embedded checkout (prepaid credit top-up).

    Returns client_secret and session_id for embedding a Stripe form in the frontend.
    """
    org_id = _require_org_id(payload, "No org_id in token — top-up requires an org-scoped credential.")
    user_id: str = payload.get("sub", "")
    session_data = await create_stripe_embedded_session(org_id, user_id, body.amount_cents, body.return_url)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=session_data,
    )


_MAX_STRIPE_WEBHOOK_PAYLOAD = 1 * 1024 * 1024  # 1 MB — Stripe events are never this large


@router.post("/billing/topup/webhook", include_in_schema=False)
async def billing_topup_webhook(request: Request) -> JSONResponse:
    """Stripe webhook receiver for checkout.session.completed events."""
    import stripe as _stripe  # noqa: PLC0415

    client_ip = request.client.host if request.client else "unknown"
    allowed, _, _ = await _rate_limit._check_rate_limit(f"webhook:{client_ip}", settings.rate_limit_webhook_rpm)
    if not allowed:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Too Many Requests"},
        )

    payload = await request.body()
    if len(payload) > _MAX_STRIPE_WEBHOOK_PAYLOAD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payload too large")
    sig_header = request.headers.get("stripe-signature", "")
    try:
        await handle_stripe_webhook(payload, sig_header)
    except _stripe.SignatureVerificationError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe signature")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload")
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})


@router.get("/billing/topup/stripe/status", tags=["Billing"])
async def billing_topup_stripe_status(
    session_id: str = Query(..., description="Stripe Checkout session ID"),
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Retrieve the status of a Stripe Checkout session and credit balance upon completion.

    Returns { status: 'open' | 'complete' | 'expired', new_balance_fmt?: '$X.XX' }
    new_balance_fmt is included only when status is 'complete'.

    Returns HTTP 403 if the session does not belong to the authenticated org.
    """
    import stripe as _stripe  # noqa: PLC0415

    org_id = _require_org_id(payload, "No org_id in token — status check requires an org-scoped credential.")

    try:
        status_data = await get_stripe_session_status(session_id, org_id)
        return JSONResponse(status_code=status.HTTP_200_OK, content=status_data)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session does not belong to this org",
        )
    except _stripe.error.InvalidRequestError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe session not found",
        )
    except Exception as e:
        logger.exception("stripe status check failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to check Stripe session status",
        )


# ─── USDC on-chain top-up endpoints ──────────────────────────────────────────


@router.get("/billing/topup/usdc/requirements", tags=["Billing"])
async def billing_usdc_topup_requirements(
    amount_usdc: int = Query(
        ...,
        ge=1_000_000,
        le=10_000_000_000,
        description=("Amount in atomic USDC (6 decimals). Min $1.00 = 1_000_000. Max $10,000 = 10_000_000_000."),
    ),
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Return x402 PaymentRequirements to sign for a USDC on-chain top-up.

    The client should sign the returned requirements using EIP-3009
    (same flow as /agent/run X-PAYMENT), then POST the signed
    payment_header to /billing/topup/usdc.

    Returns 503 if BILLING_ENABLED is false.
    """
    try:
        reqs = build_usdc_topup_requirements(amount_usdc)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"USDC top-up unavailable: {exc}",
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "accepts": [r.model_dump() if hasattr(r, "model_dump") else r.__dict__ for r in reqs],
            "x402Version": 2,
        },
    )


class UsdcTopupRequest(BaseModel):
    amount_usdc: int = Field(
        ...,
        ge=1_000_000,
        le=10_000_000_000,
        description="Amount in atomic USDC (6 decimals). Min $1.00 = 1_000_000.",
    )
    payment_header: str = Field(..., description="Base64-encoded signed EIP-3009 PaymentPayload (X-PAYMENT format).")


@router.post("/billing/topup/usdc", tags=["Billing"])
async def billing_topup_usdc(
    body: UsdcTopupRequest,
    payload: dict = Depends(require_auth),
) -> JSONResponse:
    """Top up org credit balance by submitting a signed USDC on-chain payment.

    The client obtains payment requirements from GET /billing/topup/usdc/requirements,
    signs them using EIP-3009 (MetaMask / wallet), and posts the base64-encoded
    payment_header here.

    The server verifies the signature, settles on-chain via the x402 facilitator,
    then credits the authenticated org's balance atomically.

    Returns 402 if signature verification fails, 409 if the tx_hash was already
    processed (duplicate submission), 503 if billing is disabled.
    """
    org_id = _require_org_id(payload, "No org_id in token — top-up requires an org-scoped credential.")

    try:
        result = await verify_and_settle_usdc_topup(body.payment_header, body.amount_usdc)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"USDC top-up unavailable: {exc}",
        )

    if not result.settled:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=result.error or "Payment verification or settlement failed.",
        )

    new_balance = await credit_usdc_topup(org_id, result.amount_usdc, result.tx_hash)
    if new_balance is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transaction {result.tx_hash} was already processed.",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "credited",
            "amount_usdc": result.amount_usdc,
            "balance_usdc": new_balance,
            "tx_hash": result.tx_hash,
        },
    )
