# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Static contract tests for the verified-outcome commitment migration."""

import re
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "110_vor_commitments.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_batch_table_is_source_agnostic():
    sql = _sql()
    block = sql.split("CREATE TABLE IF NOT EXISTS commitment_batches", 1)[1].split(");", 1)[0]
    assert "prediction" not in block.lower()
    assert "status" not in block.lower()
    assert "CHECK ((tx_hash IS NULL) = (anchor_address IS NULL))" in block
    assert "CHECK ((block_number IS NULL) = (anchored_at IS NULL))" in block


def test_prediction_table_carries_anchorable_contract():
    sql = _sql()
    for column in ("leaf_sha256", "commit_salt", "anchor_batch_id", "anchor_leaf_index"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "labeling_predictions_external_signed_chk" in sql
    assert re.search(r"external_signed_chk CHECK \(.*?\) NOT VALID;", sql, re.DOTALL)
    assert "uq_labeling_predictions_anchor_leaf" in sql


def test_committed_rows_are_guarded_by_triggers():
    sql = _sql()
    assert "BEFORE UPDATE OR DELETE ON labeling_predictions" in sql
    assert "BEFORE UPDATE OR DELETE ON commitment_batches" in sql
    assert "binding_id" not in sql.split("CREATE OR REPLACE FUNCTION vor_guard_prediction", 1)[1].split("$$;", 1)[0]


def test_migration_is_additive():
    sql = _sql()
    assert not re.search(r"\bDROP\s+(TABLE|COLUMN)\b", sql, re.IGNORECASE)
    assert not re.search(r"\b(DELETE\s+FROM|UPDATE\s+labeling_predictions|TRUNCATE)\b", sql, re.IGNORECASE)
