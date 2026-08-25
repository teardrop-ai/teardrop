# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import teardrop._background_tasks as background_tasks


@pytest.mark.anyio
async def test_recovery_audit_preserves_billing_projection(monkeypatch):
    task = SimpleNamespace(
        run_id="run-recovered",
        usage_event_id="usage-1",
        caller_org_id="org-1",
        caller_user_id="user-1",
        caller_ip="198.51.100.7",
        auth_method="email",
        context_id="ctx-1",
        id="task-1",
        cost_usdc=123,
        settlement_amount_usdc=100,
        settlement_tx="0xtx",
        billing_method="x402",
        duration_ms=42,
        error="Task interrupted by process restart; billing outcome is unknown.",
    )
    audit_mock = AsyncMock()
    monkeypatch.setattr("teardrop.routers.a2a_messages._record_inbound_event", audit_mock)

    await background_tasks._record_recovered_a2a_task_audits([task])

    audit_mock.assert_awaited_once_with(
        run_id="run-recovered",
        usage_event_id="usage-1",
        caller_org_id="org-1",
        caller_user_id="user-1",
        caller_address="",
        caller_ip="198.51.100.7",
        auth_method="email",
        context_id="ctx-1",
        task_id="task-1",
        task_state="failed",
        cost_usdc=123,
        settlement_amount_usdc=100,
        settlement_tx="0xtx",
        billing_method="x402",
        duration_ms=42,
        error="Task interrupted by process restart; billing outcome is unknown.",
    )
