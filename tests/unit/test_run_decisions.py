"""Unit tests for the decision-graph functions in teardrop/memory.py.

Covers store_run_decision, list_run_decisions, backfill_decision_outcome,
_sanitize_slots_snapshot, and _parse_decision. DB access is mocked via a
pool MagicMock, matching the conventions in tests/unit/test_memory.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import teardrop.memory as memory_module


def _pool():
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 0")
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


# ─── _sanitize_slots_snapshot ─────────────────────────────────────────────────


class TestSanitizeSlotsSnapshot:
    def test_drops_non_allowlisted_keys(self):
        result = memory_module._sanitize_slots_snapshot({"balances": {"ETH": 1}, "raw_args": {"wallet": "0xabc"}})
        assert result == {"balances": {"ETH": 1}}

    def test_returns_empty_for_non_dict(self):
        assert memory_module._sanitize_slots_snapshot(None) == {}
        assert memory_module._sanitize_slots_snapshot("not a dict") == {}  # type: ignore[arg-type]

    def test_returns_empty_when_nothing_allowlisted(self):
        assert memory_module._sanitize_slots_snapshot({"secret_token": "abc"}) == {}

    def test_returns_empty_when_snapshot_too_large(self):
        huge = {"balances": {f"TOKEN{i}": {"value_usd": i} for i in range(5000)}}
        result = memory_module._sanitize_slots_snapshot(huge)
        assert result == {}

    def test_returns_empty_on_unserializable_content(self):
        result = memory_module._sanitize_slots_snapshot({"balances": object()})
        assert result == {}


# ─── _parse_decision ──────────────────────────────────────────────────────────


class TestParseDecision:
    def test_parses_valid_decision(self):
        decision = memory_module._parse_decision(
            {"action": "flag_liquidation_risk", "reasoning": "HF below 1.05", "task_class": "risk", "confidence": 0.9}
        )
        assert decision == {
            "action": "flag_liquidation_risk",
            "reasoning": "HF below 1.05",
            "task_class": "risk",
            "confidence": 0.9,
        }

    def test_returns_none_for_non_dict(self):
        assert memory_module._parse_decision(None) is None
        assert memory_module._parse_decision("nope") is None

    def test_returns_none_when_action_and_reasoning_empty(self):
        assert memory_module._parse_decision({"task_class": "risk"}) is None

    def test_clamps_confidence_to_unit_interval(self):
        decision = memory_module._parse_decision({"action": "x", "confidence": 5})
        assert decision is not None
        assert decision["confidence"] == 1.0

    def test_confidence_none_when_not_numeric(self):
        decision = memory_module._parse_decision({"action": "x", "confidence": "high"})
        assert decision is not None
        assert decision["confidence"] is None

    def test_truncates_oversized_fields(self):
        decision = memory_module._parse_decision({"action": "a" * 200, "reasoning": "r" * 600})
        assert decision is not None
        assert len(decision["action"]) == 120
        assert len(decision["reasoning"]) == 500


# ─── store_run_decision ───────────────────────────────────────────────────────


@pytest.mark.anyio
class TestStoreRunDecision:
    async def test_stores_successfully(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value={"id": "d-1"})

        with patch.object(memory_module, "_pool", pool):
            stored = await memory_module.store_run_decision(
                org_id="org-1",
                user_id="user-1",
                run_id="run-1",
                decision={"action": "flag_risk", "reasoning": "low HF", "task_class": "risk", "confidence": 0.8},
                tool_names=["get_liquidation_risk"],
                slots={"risk": {"tier": "high"}, "raw_args": {"wallet": "0xabc"}},
            )

        assert stored is True
        # Sanitized snapshot must never include the non-allowlisted key.
        # pool.fetchrow(query, *params) -- args[0] is the SQL query string,
        # so positional param N is at args[N] (1-indexed against $N placeholders).
        call_args = pool.fetchrow.call_args.args
        assert '"raw_args"' not in call_args[9]
        assert '"risk"' in call_args[9]

    async def test_returns_false_on_duplicate(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(return_value=None)  # ON CONFLICT DO NOTHING -> no row

        with patch.object(memory_module, "_pool", pool):
            stored = await memory_module.store_run_decision(
                org_id="org-1",
                user_id="user-1",
                run_id="run-1",
                decision={"action": "a", "reasoning": "r"},
            )

        assert stored is False

    async def test_never_raises(self, test_settings):
        pool = _pool()
        pool.fetchrow = AsyncMock(side_effect=Exception("db down"))

        with patch.object(memory_module, "_pool", pool):
            stored = await memory_module.store_run_decision(
                org_id="org-1",
                user_id="user-1",
                run_id="run-1",
                decision={"action": "a", "reasoning": "r"},
            )

        assert stored is False


# ─── list_run_decisions ───────────────────────────────────────────────────────


@pytest.mark.anyio
class TestListRunDecisions:
    async def test_returns_entries_without_cursor(self, test_settings):
        pool = _pool()
        now = datetime.now(timezone.utc)
        pool.fetch = AsyncMock(
            return_value=[
                {
                    "id": "d-1",
                    "run_id": "run-1",
                    "task_class": "risk",
                    "action": "flag_risk",
                    "reasoning": "low HF",
                    "confidence": 0.8,
                    "tool_names": ["get_liquidation_risk"],
                    "outcome": 0,
                    "outcome_source": "",
                    "created_at": now,
                }
            ]
        )

        with patch.object(memory_module, "_pool", pool):
            results = await memory_module.list_run_decisions("org-1")

        assert len(results) == 1
        assert results[0]["run_id"] == "run-1"

    async def test_returns_entries_with_cursor(self, test_settings):
        pool = _pool()
        pool.fetch = AsyncMock(return_value=[])
        cursor = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with patch.object(memory_module, "_pool", pool):
            results = await memory_module.list_run_decisions("org-1", cursor=cursor)

        assert results == []
        call_args = pool.fetch.call_args.args
        assert cursor in call_args


# ─── backfill_decision_outcome ────────────────────────────────────────────────


@pytest.mark.anyio
class TestBackfillDecisionOutcome:
    async def test_returns_true_on_success(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="UPDATE 1")

        with patch.object(memory_module, "_pool", pool):
            updated = await memory_module.backfill_decision_outcome("run-1", "org-1", 1)

        assert updated is True

    async def test_returns_false_when_no_matching_row(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(return_value="UPDATE 0")

        with patch.object(memory_module, "_pool", pool):
            updated = await memory_module.backfill_decision_outcome("run-1", "org-1", 1)

        assert updated is False

    async def test_never_raises(self, test_settings):
        pool = _pool()
        pool.execute = AsyncMock(side_effect=Exception("db down"))

        with patch.object(memory_module, "_pool", pool):
            updated = await memory_module.backfill_decision_outcome("run-1", "org-1", 1)

        assert updated is False
