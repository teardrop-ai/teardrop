# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from pathlib import Path


def test_mcp_call_events_migration_preserves_audit_invariants():
    sql = Path("migrations/versions/107_mcp_call_events.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS mcp_call_events" in sql
    assert "id                TEXT PRIMARY KEY" in sql
    assert "billing_method IN ('x402', 'credit')" in sql
    assert "cost_usdc >= 0" in sql
    assert "settlement_status IN ('settled', 'failed')" in sql
    assert "idx_mcp_call_events_payer" in sql
    assert "WHERE payer_address <> ''" in sql
