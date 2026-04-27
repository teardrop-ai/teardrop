"""Unit tests for marketplace.py — auto-sweep worker (Phase 0.3).

All DB interactions and CDP calls are mocked; no live Postgres required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace import (
    AuthorWithdrawal,
    _marketplace_sweep_loop,
    _sweep_backoff_seconds,
    _sweep_withdrawal_id,
    marketplace_sweep_once,
)

_NOW = datetime.now(timezone.utc)
_VALID_ADDR = "0x1234567890123456789012345678901234567890"


# ─── Helper ──────────────────────────────────────────────────────────────────


def _make_withdrawal(
    withdrawal_id: str = "wd-1",
    org_id: str = "org-1",
    status: str = "settled",
    sweep_attempt_count: int = 0,
    last_sweep_error: str = "",
    next_sweep_at: datetime | None = None,
) -> AuthorWithdrawal:
    return AuthorWithdrawal(
        id=withdrawal_id,
        org_id=org_id,
        amount_usdc=500_000,
        tx_hash="0xdeadbeef",
        wallet=_VALID_ADDR,
        status=status,
        sweep_attempt_count=sweep_attempt_count,
        last_sweep_error=last_sweep_error,
        next_sweep_at=next_sweep_at,
        created_at=_NOW,
        settled_at=_NOW if status == "settled" else None,
    )


# ─── _sweep_withdrawal_id ────────────────────────────────────────────────────


class TestSweepWithdrawalId:
    def test_deterministic_same_inputs(self):
        id1 = _sweep_withdrawal_id("org-abc", 1_000_000)
        id2 = _sweep_withdrawal_id("org-abc", 1_000_000)
        assert id1 == id2

    def test_different_orgs_produce_different_ids(self):
        id1 = _sweep_withdrawal_id("org-a", 1_000_000)
        id2 = _sweep_withdrawal_id("org-b", 1_000_000)
        assert id1 != id2

    def test_different_epoch_hours_produce_different_ids(self):
        id1 = _sweep_withdrawal_id("org-a", 1_000_000)
        id2 = _sweep_withdrawal_id("org-a", 1_000_001)
        assert id1 != id2

    def test_valid_uuid_format(self):
        import re

        uid = _sweep_withdrawal_id("org-xyz", 999)
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$",
            uid,
        ), f"Not a valid UUID v5 shape: {uid}"


# ─── _sweep_backoff_seconds ───────────────────────────────────────────────────


class TestSweepBackoffSeconds:
    def test_attempt_1_is_two_minutes(self):
        assert _sweep_backoff_seconds(1) == 120  # 2^1 * 60

    def test_attempt_2_is_four_minutes(self):
        assert _sweep_backoff_seconds(2) == 240  # 2^2 * 60

    def test_large_attempt_capped_at_24h(self):
        # 2^20 * 60 >> 86400 — must clamp
        assert _sweep_backoff_seconds(20) == 86_400

    def test_attempt_0_is_60s(self):
        assert _sweep_backoff_seconds(0) == 60  # 2^0 * 60


# ─── marketplace_sweep_once ──────────────────────────────────────────────────


class TestMarketplaceSweepOnce:
    @pytest.mark.anyio
    async def test_no_qualifying_orgs_returns_zero(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        count = await marketplace_sweep_once()
        assert count == 0

    @pytest.mark.anyio
    async def test_skips_org_already_being_processed(self, monkeypatch):
        """The SQL query itself filters out blocked orgs; empty result → 0 processed."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        count = await marketplace_sweep_once()
        assert count == 0
        # Confirm we issued a query that references next_sweep_at (dedup logic)
        call_sql: str = mock_pool.fetch.call_args[0][0]
        assert "next_sweep_at" in call_sql

    @pytest.mark.anyio
    async def test_processes_qualifying_org_successfully(self, monkeypatch):
        org_row = {"org_id": "org-1", "total": 500_000}
        settled_wd = _make_withdrawal(status="settled")

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[org_row])
        # No existing withdrawal for the deterministic ID
        mock_pool.fetchrow = AsyncMock(return_value=None)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        # get_author_config returns a config
        mock_config = MagicMock()
        mock_config.settlement_wallet = _VALID_ADDR
        monkeypatch.setattr("marketplace.get_author_config", AsyncMock(return_value=mock_config))
        monkeypatch.setattr("marketplace.process_withdrawal", AsyncMock(return_value=settled_wd))

        count = await marketplace_sweep_once()
        assert count == 1

    @pytest.mark.anyio
    async def test_skips_org_with_no_author_config(self, monkeypatch):
        org_row = {"org_id": "org-1", "total": 500_000}

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[org_row])
        mock_pool.fetchrow = AsyncMock(return_value=None)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        monkeypatch.setattr("marketplace.get_author_config", AsyncMock(return_value=None))

        count = await marketplace_sweep_once()
        assert count == 0

    @pytest.mark.anyio
    async def test_already_settled_in_epoch_counts_as_success(self, monkeypatch):
        org_row = {"org_id": "org-1", "total": 500_000}
        existing_settled = {"id": "wd-existing", "status": "settled"}

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[org_row])
        mock_pool.fetchrow = AsyncMock(return_value=existing_settled)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        count = await marketplace_sweep_once()
        assert count == 1

    @pytest.mark.anyio
    async def test_cdp_failure_sets_backoff_metadata(self, monkeypatch):
        """When process_withdrawal returns status='failed', retry metadata is written."""
        org_row = {"org_id": "org-1", "total": 500_000}
        failed_wd = _make_withdrawal(status="failed", last_sweep_error="CDP error")

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[org_row])
        mock_pool.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing withdrawal
                {"sweep_attempt_count": 0},  # read attempt count after failure
            ]
        )
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        mock_config = MagicMock()
        mock_config.settlement_wallet = _VALID_ADDR
        monkeypatch.setattr("marketplace.get_author_config", AsyncMock(return_value=mock_config))
        monkeypatch.setattr("marketplace.process_withdrawal", AsyncMock(return_value=failed_wd))

        count = await marketplace_sweep_once()
        assert count == 0

        # An UPDATE with backoff should have been executed
        execute_calls = mock_pool.execute.call_args_list
        update_calls = [c for c in execute_calls if "next_sweep_at" in str(c)]
        assert update_calls, "Expected a backoff UPDATE with next_sweep_at"

    @pytest.mark.anyio
    async def test_exhausts_withdrawal_after_max_retries(self, monkeypatch):
        """At max_retries the status should flip to 'exhausted'."""
        from config import get_settings

        settings = get_settings()
        max_retries = settings.marketplace_max_sweep_retries

        org_row = {"org_id": "org-1", "total": 500_000}
        failed_wd = _make_withdrawal(status="failed", last_sweep_error="CDP unavailable")

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[org_row])
        mock_pool.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing withdrawal
                {"sweep_attempt_count": max_retries - 1},  # already at limit
            ]
        )
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        mock_config = MagicMock()
        mock_config.settlement_wallet = _VALID_ADDR
        monkeypatch.setattr("marketplace.get_author_config", AsyncMock(return_value=mock_config))
        monkeypatch.setattr("marketplace.process_withdrawal", AsyncMock(return_value=failed_wd))

        count = await marketplace_sweep_once()
        assert count == 0

        # UPDATE to 'exhausted' must have been executed
        execute_calls = mock_pool.execute.call_args_list
        exhausted_calls = [c for c in execute_calls if "'exhausted'" in str(c)]
        assert exhausted_calls, "Expected an UPDATE to status='exhausted'"

    @pytest.mark.anyio
    async def test_unexpected_exception_does_not_abort_other_orgs(self, monkeypatch):
        """An unexpected crash for one org should not prevent processing of others."""
        org_row_1 = {"org_id": "org-1", "total": 500_000}
        org_row_2 = {"org_id": "org-2", "total": 500_000}
        settled_wd = _make_withdrawal(org_id="org-2", status="settled")

        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[org_row_1, org_row_2])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        # First org raises; second succeeds.
        get_config_returns = [RuntimeError("unexpected!"), MagicMock(settlement_wallet=_VALID_ADDR)]

        async def _side_effect(org_id: str):
            val = get_config_returns.pop(0)
            if isinstance(val, Exception):
                raise val
            return val

        monkeypatch.setattr("marketplace.get_author_config", _side_effect)
        mock_pool.fetchrow = AsyncMock(return_value=None)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace.process_withdrawal", AsyncMock(return_value=settled_wd))

        count = await marketplace_sweep_once()
        assert count == 1


# ─── _marketplace_sweep_loop ─────────────────────────────────────────────────


class TestMarketplaceSweepLoop:
    @pytest.mark.anyio
    async def test_cancelled_error_propagates(self, monkeypatch):
        """CancelledError must escape the loop so asyncio can clean up the task."""
        call_count = 0

        async def _fake_sleep(seconds: float) -> None:  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.CancelledError

        monkeypatch.setattr("marketplace.asyncio.sleep", _fake_sleep)
        monkeypatch.setattr("marketplace.marketplace_sweep_once", AsyncMock(return_value=0))

        with pytest.raises(asyncio.CancelledError):
            await _marketplace_sweep_loop()

    @pytest.mark.anyio
    async def test_ordinary_exception_does_not_kill_loop(self, monkeypatch):
        """A non-CancelledError crash in sweep_once should be caught and the loop continue."""
        sleep_call_count = 0

        async def _fake_sleep(seconds: float) -> None:  # noqa: ARG001
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 3:
                raise asyncio.CancelledError

        sweep_call_count = 0

        async def _fake_sweep() -> int:
            nonlocal sweep_call_count
            sweep_call_count += 1
            if sweep_call_count == 1:
                raise RuntimeError("transient failure")
            return 0

        monkeypatch.setattr("marketplace.asyncio.sleep", _fake_sleep)
        monkeypatch.setattr("marketplace.marketplace_sweep_once", _fake_sweep)

        with pytest.raises(asyncio.CancelledError):
            await _marketplace_sweep_loop()

        # Sleep was called 3 times, sweep_once was called at least twice
        assert sleep_call_count == 3
        assert sweep_call_count == 2
