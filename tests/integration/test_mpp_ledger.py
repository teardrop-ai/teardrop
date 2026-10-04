# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Migration 119: MPP outcomes must persist on both MCP ledgers (CHECKs used to drop them)."""

from __future__ import annotations

import pytest

from billing.charges import record_charge
from migrations.runner import apply_pending
from shared.db_pool import create_pool

_TX = "0x" + "11" * 32
_PAYER = "0x" + "42" * 20


@pytest.fixture
async def ledger_pool(docker_postgres: str):
    pool = await create_pool(docker_postgres, min_size=1, max_size=3, name="integration-mpp-ledger")
    await apply_pending(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE mcp_call_events, billing_charges RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


@pytest.mark.asyncio
async def test_mpp_outcomes_persist_on_both_ledgers(ledger_pool, monkeypatch):
    from teardrop import usage

    monkeypatch.setattr(usage, "_pool", ledger_pool)
    monkeypatch.setattr("billing.charges._has_pool", lambda: True)
    monkeypatch.setattr("billing.charges._get_pool", lambda: ledger_pool)

    await usage.record_mcp_call_event("mpp-call-1", "", _PAYER, "get_price", "mpp", 10_000, "settled", _TX)
    charge_id = await record_charge(
        source="mcp",
        invocation_id="mpp-call-1",
        billing_method="mpp",
        amount_usdc=10_000,
        status="settled",
        settled_amount_usdc=10_000,
        settlement_tx=_TX,
        payer_address=_PAYER,
        capability="get_price",
    )

    event = await ledger_pool.fetchrow("SELECT billing_method, settlement_tx FROM mcp_call_events WHERE id = 'mpp-call-1'")
    charge = await ledger_pool.fetchrow(
        "SELECT billing_method, status, payer_address FROM billing_charges WHERE id = $1", charge_id
    )
    assert dict(event) == {"billing_method": "mpp", "settlement_tx": _TX}
    assert dict(charge) == {"billing_method": "mpp", "status": "settled", "payer_address": _PAYER}

    with pytest.raises(Exception, match="check constraint"):
        await ledger_pool.execute(
            "INSERT INTO mcp_call_events (id, org_id, payer_address, tool_name, billing_method, cost_usdc,"
            " settlement_status) VALUES ('bad', '', '', 't', 'stripe', 1, 'settled')"
        )
