# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from pathlib import Path


def test_billing_charges_migration_preserves_ledger_invariants():
    sql = Path("migrations/versions/115_billing_charges.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS billing_charges" in sql
    assert "UNIQUE (source, invocation_id)" in sql
    assert "amount_usdc         BIGINT" in sql
    assert "billing_method IN ('credit', 'x402')" in sql
    assert "status IN ('settled', 'failed', 'reverted')" in sql
    assert "BEFORE UPDATE OR DELETE ON billing_charges" in sql
    assert "OLD.status = 'failed' AND NEW.status = 'settled'" in sql
    assert "OLD.status = 'settled' AND NEW.status = 'reverted'" in sql
    assert "ADD COLUMN IF NOT EXISTS charge_id TEXT NOT NULL DEFAULT ''" in sql
    assert "payment_payload" not in sql.split("CREATE TABLE", 1)[1].split(");", 1)[0]
