# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Static contract tests for the machine-org provisioning migration."""

import re
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "097_machine_org_provisioning.sql"


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_machine_org_migration_does_not_rewrite_credit_limits() -> None:
    sql = _migration_sql()

    assert "spending_limit_usdc" not in sql
    assert not re.search(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+org_credits\b", sql, re.IGNORECASE)
    assert "LEAST(" not in sql


def test_machine_org_provenance_backfill_is_idempotent() -> None:
    sql = _migration_sql()

    assert "CREATE TABLE IF NOT EXISTS org_provisioning_events" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_provisioning_events_payment_ref" in sql
    assert "INSERT INTO org_provisioning_events" in sql
    assert "WHERE o.acquisition_source IN ('siwe', 'x402')" in sql
    assert "WHERE e.id = md5('097:legacy:' || w.id)" in sql
    assert "NOT EXISTS (" in sql
