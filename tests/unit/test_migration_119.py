# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from pathlib import Path


def test_mpp_billing_method_migration_is_additive():
    sql = Path("migrations/versions/119_mpp_billing_method.sql").read_text(encoding="utf-8")

    assert "CHECK (billing_method IN ('x402', 'credit', 'mpp'))" in sql
    assert "CHECK (billing_method IN ('credit', 'x402', 'mpp'))" in sql
    assert "pending_settlements" not in sql.split("$$;", 1)[1]
    for destructive in ("DROP TABLE", "DELETE FROM", "UPDATE ", "TRUNCATE"):
        assert destructive not in sql
