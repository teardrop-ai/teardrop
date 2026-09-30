# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unified per-invocation charge ledger (``billing_charges``).

Dual-write phase: every settlement path records its outcome here in addition to
its protocol-specific table. Writes are best-effort and never raise, because
the money movement they describe has already happened.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from billing.context import _get_pool, _has_pool
from shared.db_pool import PgConnection

logger = logging.getLogger(__name__)

ChargeSource = Literal["api", "schedule", "trigger", "a2a", "mcp", "mcp_v1"]
ChargeMethod = Literal["credit", "x402"]
ChargeStatus = Literal["settled", "failed"]

# Fixed namespace so a charge id is reproducible from its (source, invocation_id) key.
_CHARGE_NAMESPACE = uuid.UUID("5b0f5a3e-6c1d-4f5e-9a57-3c2b8f1d7e42")


def charge_id_for(source: str, invocation_id: str) -> str:
    """Return the deterministic charge id for one billed invocation."""
    return str(uuid.uuid5(_CHARGE_NAMESPACE, f"{source}:{invocation_id}"))


async def record_charge(
    *,
    source: ChargeSource,
    invocation_id: str,
    billing_method: ChargeMethod,
    amount_usdc: int,
    status: ChargeStatus,
    settled_amount_usdc: int = 0,
    settlement_tx: str = "",
    org_id: str = "",
    principal_id: str = "",
    payer_address: str = "",
    capability: str = "",
    usage_event_id: str = "",
) -> str:
    """Insert one charge outcome (idempotent per invocation) and return its id."""
    charge_id = charge_id_for(source, invocation_id)
    if not _has_pool():
        return charge_id
    try:
        await _get_pool().execute(
            """
            INSERT INTO billing_charges
                (id, source, invocation_id, usage_event_id, org_id, principal_id, payer_address,
                 capability, billing_method, amount_usdc, settled_amount_usdc, status, settlement_tx)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (source, invocation_id) DO NOTHING
            """,
            charge_id,
            source,
            invocation_id,
            usage_event_id,
            org_id or "",
            principal_id or "",
            (payer_address or "").strip().lower(),
            capability,
            billing_method,
            max(0, int(amount_usdc)),
            max(0, int(settled_amount_usdc)) if status == "settled" else 0,
            status,
            settlement_tx if status == "settled" else "",
        )
    except Exception as exc:
        logger.error("Failed to record billing charge source=%s error_type=%s", source, type(exc).__name__)
    return charge_id


async def mark_charge_settled(conn: PgConnection, charge_id: str, settled_amount_usdc: int) -> None:
    """Settle a failed charge inside the caller's retry transaction."""
    await conn.execute(
        """
        UPDATE billing_charges
        SET status = 'settled', settled_amount_usdc = $2, updated_at = NOW()
        WHERE id = $1 AND status = 'failed'
        """,
        charge_id,
        max(0, int(settled_amount_usdc)),
    )


async def mark_charge_reverted(usage_event_id: str, settlement_tx: str) -> None:
    """Mark a settled x402 charge reverted after a failed on-chain receipt check."""
    if not usage_event_id or not settlement_tx or not _has_pool():
        return
    try:
        await _get_pool().execute(
            """
            UPDATE billing_charges
            SET status = 'reverted', updated_at = NOW()
            WHERE usage_event_id = $1 AND settlement_tx = $2 AND status = 'settled'
            """,
            usage_event_id,
            settlement_tx,
        )
    except Exception as exc:
        logger.error("Failed to mark billing charge reverted error_type=%s", type(exc).__name__)
