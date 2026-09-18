# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Shared retention-settings stub for unit and integration tests.

Single source of truth so ``teardrop/retention.py`` settings access can never
drift from test stubs again; ``tests/unit/test_retention.py`` enforces that
every attribute accessed by the sweep module exists here.
"""

from __future__ import annotations

from types import SimpleNamespace


def retention_settings(**overrides: object) -> SimpleNamespace:
    """Build a retention settings stub matching ``teardrop/config.py`` defaults.

    Includes every attribute accessed directly or via ``getattr`` in
    ``teardrop/retention.py``. A drift-guard test asserts this stays complete.
    """
    values: dict[str, object] = {
        "checkpoint_ttl_days": 45,
        "scheduled_run_results_ttl_days": 30,
        "org_tool_execution_events_ttl_days": 90,
        "telemetry_run_starts_ttl_days": 120,
        "discovery_stage_counts_ttl_days": 180,
        "retention_sweep_batch_size": 2,
        "labeling_retention_days": 0,
        "a2a_inbound_task_ttl_days": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
