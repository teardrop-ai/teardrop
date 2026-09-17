# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from pathlib import Path


def test_discovery_stage_counts_migration_preserves_aggregate_only_invariants():
    sql = Path("migrations/versions/108_discovery_stage_counts.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS discovery_stage_counts" in sql
    assert "PRIMARY KEY (surface, bucket_hour)" in sql
    assert "count >= 0" in sql

    # Aggregate-only: the table definition must contain no per-request identity columns.
    create_block = sql.split("CREATE TABLE IF NOT EXISTS discovery_stage_counts", 1)[1].split(");", 1)[0]
    assert "ip" not in create_block.lower()
    assert "user_agent" not in create_block.lower()
    assert "referer" not in create_block.lower()
