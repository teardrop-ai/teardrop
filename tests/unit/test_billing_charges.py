# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for the unified billing charge ledger helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import billing
import billing.charges as charges
import billing.settlement as settlement


def test_charge_id_is_deterministic_per_source_and_invocation():
    assert charges.charge_id_for("mcp", "call-1") == charges.charge_id_for("mcp", "call-1")
    assert charges.charge_id_for("mcp", "call-1") != charges.charge_id_for("mcp_v1", "call-1")
    assert charges.charge_id_for("api", "run-1") != charges.charge_id_for("api", "run-2")


@pytest.mark.anyio
async def test_record_charge_is_idempotent_and_sanitized():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        charge_id = await charges.record_charge(
            source="mcp",
            invocation_id="call-1",
            org_id="",
            payer_address=" 0xABCdef ",
            capability="platform/get_price",
            billing_method="x402",
            amount_usdc=2_000,
            status="settled",
            settled_amount_usdc=2_000,
            settlement_tx="0xtx",
        )

    assert charge_id == charges.charge_id_for("mcp", "call-1")
    sql, *args = pool.execute.await_args.args
    assert "ON CONFLICT (source, invocation_id) DO NOTHING" in sql
    assert args == [
        charge_id,
        "mcp",
        "call-1",
        "",
        "",
        "",
        "0xabcdef",
        "platform/get_price",
        "x402",
        2_000,
        2_000,
        "settled",
        "0xtx",
    ]


@pytest.mark.anyio
async def test_failed_charge_never_records_settled_amount_or_tx():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        await charges.record_charge(
            source="api",
            invocation_id="run-1",
            billing_method="credit",
            amount_usdc=5_000,
            status="failed",
            settled_amount_usdc=5_000,
            settlement_tx="0xignored",
        )

    args = pool.execute.await_args.args[1:]
    assert args[10] == 0
    assert args[11] == "failed"
    assert args[12] == ""


@pytest.mark.anyio
async def test_record_charge_never_raises_and_skips_without_pool():
    pool = MagicMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        charge_id = await charges.record_charge(
            source="api", invocation_id="run-1", billing_method="credit", amount_usdc=1, status="settled"
        )
    assert charge_id == charges.charge_id_for("api", "run-1")

    with patch.object(charges, "_has_pool", return_value=False):
        assert await charges.record_charge(
            source="api", invocation_id="run-2", billing_method="credit", amount_usdc=1, status="settled"
        ) == charges.charge_id_for("api", "run-2")


@pytest.mark.anyio
async def test_status_transitions_are_guarded():
    conn = MagicMock()
    conn.execute = AsyncMock()
    await charges.mark_charge_settled(conn, "charge-1", 700)
    sql, charge_id, amount = conn.execute.await_args.args
    assert "WHERE id = $1 AND status = 'failed'" in sql
    assert (charge_id, amount) == ("charge-1", 700)

    pool = MagicMock()
    pool.execute = AsyncMock()
    with (
        patch.object(charges, "_has_pool", return_value=True),
        patch.object(charges, "_get_pool", return_value=pool),
    ):
        await charges.mark_charge_reverted("ue-1", "0xtx")
        await charges.mark_charge_reverted("ue-1", "")
    pool.execute.assert_awaited_once()
    assert "status = 'settled'" in pool.execute.await_args.args[0]


@pytest.mark.anyio
async def test_root_enqueue_wrapper_forwards_principal_and_charge():
    """Regression: the root wrapper rejected principal_id, so agent-run credit retries raised TypeError."""
    with patch.object(billing, "_ENQUEUE_FAILED_SETTLEMENT_ORIG", AsyncMock()) as enqueue:
        await billing.enqueue_failed_settlement(
            "ue-1", "org-1", "run-1", "credit", 500, principal_id="user-1", charge_id="charge-1"
        )

    assert enqueue.await_args.kwargs == {"principal_id": "user-1", "charge_id": "charge-1"}


@pytest.mark.anyio
async def test_enqueue_persists_charge_link():
    pool = MagicMock()
    pool.execute = AsyncMock()

    with patch.object(settlement, "_get_pool", return_value=pool):
        await settlement.enqueue_failed_settlement("ue-1", "org-1", "run-1", "credit", 500, charge_id="charge-1")

    sql, *args = pool.execute.await_args.args
    assert "charge_id" in sql
    assert args[-1] == "charge-1"
