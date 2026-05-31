"""Unit tests for A2A delegation billing features.

Covers:
- billing.py: check_delegation_budget, apply_platform_fee, fund_delegation,
  record_delegation_event, get_treasury_signer
- a2a_client.py: check_delegation_allowed, send_message_with_payment
- delegate_to_agent: billing integration (allowlist enforcement, budget check)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from billing import apply_platform_fee, check_delegation_budget
from teardrop.a2a_client import (
    A2AAgentCard,
    A2ASendMessageResponse,
    A2ATask,
    A2ATaskStatus,
    check_delegation_allowed,
)
from tools.definitions.delegate_to_agent import delegate_to_agent

_A2A_MOD = "teardrop.a2a_client"
_BILLING_MOD = "billing"

pytestmark = pytest.mark.anyio


# ─── apply_platform_fee ──────────────────────────────────────────────────────


class TestApplyPlatformFee:
    def test_default_500bps(self, test_settings, monkeypatch):
        """500 bps (5%) fee: 10000 → 10500."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "500")
        _config.get_settings.cache_clear()
        assert apply_platform_fee(10_000) == 10_500

    def test_zero_fee(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()
        assert apply_platform_fee(10_000) == 10_000

    def test_1000bps(self, test_settings, monkeypatch):
        """1000 bps (10%): 100000 → 110000."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "1000")
        _config.get_settings.cache_clear()
        assert apply_platform_fee(100_000) == 110_000


# ─── check_delegation_budget ─────────────────────────────────────────────────


class TestCheckDelegationBudget:
    async def test_billing_disabled_allows_all(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "false")
        _config.get_settings.cache_clear()
        result = await check_delegation_budget("org-1", 999_999)
        assert result is None

    async def test_exceeds_global_cap(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "50000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 1_000_000, "spending_limit_usdc": 0, "is_paused": False})
        with patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool):
            result = await check_delegation_budget("org-1", 60_000)
            assert result is not None
            assert "cap" in result.lower()

    async def test_insufficient_credits(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 5_000, "spending_limit_usdc": 0, "is_paused": False})
        with patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool):
            result = await check_delegation_budget("org-1", 10_000)
            assert result is not None
            assert "insufficient" in result.lower()

    async def test_sufficient_credits(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 0, "is_paused": False})
        with patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool):
            result = await check_delegation_budget("org-1", 10_000)
            assert result is None

    async def test_paused_org_blocked(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 0, "is_paused": True})
        with patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool):
            result = await check_delegation_budget("org-1", 10_000)
            assert result is not None
            assert "paused" in result.lower()

    async def test_spending_limit_exceeded(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(
            side_effect=[
                {"balance_usdc": 100_000, "spending_limit_usdc": 50_000, "is_paused": False},
                {"daily_spend": 40_000},
            ]
        )
        with patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool):
            result = await check_delegation_budget("org-1", 20_000)
            assert result is not None
            assert "limit" in result.lower()

    async def test_spending_limit_zero_skips_limit_check(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 0, "is_paused": False})
        with patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool):
            result = await check_delegation_budget("org-1", 10_000)
            assert result is None
        assert mock_pool.fetchrow.await_count == 1

    async def test_budget_check_bypasses_display_cache(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value={"balance_usdc": 100_000, "spending_limit_usdc": 50_000, "is_paused": False})
        with (
            patch(f"{_BILLING_MOD}._get_pool", return_value=mock_pool),
            patch(f"{_BILLING_MOD}._get_daily_debit_spend", new=AsyncMock(return_value=5_000)),
            patch(f"{_BILLING_MOD}._get_daily_spend_cache") as mock_cache_factory,
        ):
            result = await check_delegation_budget("org-1", 10_000)
            assert result is None

        mock_cache_factory.assert_not_called()


# ─── check_delegation_allowed ─────────────────────────────────────────────────


class TestCheckDelegationAllowed:
    async def test_allowed_agent(self):
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(
            return_value={
                "id": "a-1",
                "agent_url": "https://agent.example.com",
                "label": "Test",
                "max_cost_usdc": 50_000,
                "require_x402": False,
                "created_at": None,
            }
        )
        allowed, row = await check_delegation_allowed("org-1", "https://agent.example.com", pool)
        assert allowed is True
        assert row["max_cost_usdc"] == 50_000

    async def test_not_allowed(self):
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        allowed, row = await check_delegation_allowed("org-1", "https://unknown.agent.com", pool)
        assert allowed is False
        assert row is None


# ─── get_treasury_signer ─────────────────────────────────────────────────────


class TestGetTreasurySigner:
    def test_missing_key_raises(self, test_settings, monkeypatch):
        import teardrop.config as _config

        monkeypatch.setenv("X402_TREASURY_PRIVATE_KEY", "")
        _config.get_settings.cache_clear()

        from billing import get_treasury_signer

        with pytest.raises(RuntimeError, match="not configured"):
            get_treasury_signer()


# ─── delegate_to_agent with billing ──────────────────────────────────────────


class TestDelegateToAgentBilling:
    async def test_allowlist_rejection(self, test_settings, monkeypatch):
        """When agent is not in allowlist and billing is enabled, returns error."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)

        config = {
            "configurable": {
                "org_id": "org-1",
                "run_id": "run-1",
                "db_pool": mock_pool,
            }
        }

        with patch(f"{_A2A_MOD}.validate_url", return_value=None):
            result = await delegate_to_agent("https://agent.example.com", "test", config=config)
        assert result["status"] == "failed"
        assert "allowed" in result["error"].lower()

    async def test_budget_rejection(self, test_settings, monkeypatch):
        """When org lacks budget, returns error before contacting remote agent."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "100000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": "a-1",
                "agent_url": "https://agent.example.com",
                "label": "Test",
                "max_cost_usdc": 0,
                "require_x402": False,
                "created_at": None,
            }
        )

        config = {
            "configurable": {
                "org_id": "org-1",
                "run_id": "run-1",
                "db_pool": mock_pool,
            }
        }

        with (
            patch(f"{_A2A_MOD}.validate_url", return_value=None),
            patch(
                f"{_BILLING_MOD}._get_pool",
                return_value=AsyncMock(
                    fetchrow=AsyncMock(return_value={"balance_usdc": 0, "spending_limit_usdc": 0, "is_paused": False})
                ),
            ),
        ):
            result = await delegate_to_agent("https://agent.example.com", "test", config=config)
        assert result["status"] == "failed"
        assert "insufficient" in result["error"].lower()

    async def test_happy_path_with_billing(self, test_settings, monkeypatch):
        """Successful delegation debits credits and records event."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "500")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": "a-1",
                "agent_url": "https://agent.example.com",
                "label": "Test",
                "max_cost_usdc": 50000,
                "require_x402": False,
                "created_at": None,
            }
        )

        mock_card = A2AAgentCard(name="BilledAgent", description="A billed agent")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(
                id="task-001",
                status=A2ATaskStatus(state="completed"),
                artifacts=[],
            ),
            raw={},
        )

        config = {
            "configurable": {
                "org_id": "org-1",
                "run_id": "run-1",
                "db_pool": mock_pool,
            }
        }

        mock_fund = AsyncMock(return_value=True)
        mock_record = AsyncMock()

        with (
            patch(f"{_A2A_MOD}.validate_url", return_value=None),
            patch(
                f"{_BILLING_MOD}._get_pool",
                return_value=AsyncMock(
                    fetchrow=AsyncMock(return_value={"balance_usdc": 1_000_000, "spending_limit_usdc": 0, "is_paused": False})
                ),
            ),
            patch(f"{_A2A_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_A2A_MOD}.send_message", AsyncMock(return_value=mock_response)),
            patch(f"{_A2A_MOD}.extract_result_text", return_value="Result text"),
            patch(f"{_BILLING_MOD}.fund_delegation", mock_fund),
            patch(f"{_BILLING_MOD}.record_delegation_event", mock_record),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do work", config=config)

        assert result["status"] == "completed"
        assert result["cost_usdc"] > 0
        mock_fund.assert_called_once()
        mock_record.assert_called_once()

    async def test_cost_usdc_in_output(self, test_settings, monkeypatch):
        """Output schema includes cost_usdc field even without billing."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "false")
        _config.get_settings.cache_clear()

        mock_card = A2AAgentCard(name="FreeAgent", description="No billing")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(
                id="task-001",
                status=A2ATaskStatus(state="completed"),
                artifacts=[],
            ),
            raw={},
        )

        with (
            patch(f"{_A2A_MOD}.validate_url", return_value=None),
            patch(f"{_A2A_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_A2A_MOD}.send_message", AsyncMock(return_value=mock_response)),
            patch(f"{_A2A_MOD}.extract_result_text", return_value="Done"),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do stuff")

        assert result["cost_usdc"] == 0
        assert result["status"] == "completed"

    async def test_pre_debit_failure_aborts_dispatch(self, test_settings, monkeypatch):
        """When the pre-debit fails, the remote agent is never contacted."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": "a-1",
                "agent_url": "https://agent.example.com",
                "label": "Test",
                "max_cost_usdc": 50000,
                "require_x402": False,
                "created_at": None,
            }
        )

        mock_card = A2AAgentCard(name="BilledAgent", description="A billed agent")
        config = {"configurable": {"org_id": "org-1", "run_id": "run-1", "db_pool": mock_pool}}

        mock_fund = AsyncMock(return_value=False)  # debit fails
        mock_send = AsyncMock()

        with (
            patch(f"{_A2A_MOD}.validate_url", return_value=None),
            patch(
                f"{_BILLING_MOD}._get_pool",
                return_value=AsyncMock(
                    fetchrow=AsyncMock(
                        return_value={"balance_usdc": 1_000_000, "spending_limit_usdc": 0, "is_paused": False}
                    )
                ),
            ),
            patch(f"{_A2A_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_A2A_MOD}.send_message", mock_send),
            patch(f"{_BILLING_MOD}.fund_delegation", mock_fund),
            patch(f"{_BILLING_MOD}.record_delegation_event", AsyncMock()),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do work", config=config)

        assert result["status"] == "failed"
        assert result["cost_usdc"] == 0
        mock_fund.assert_called_once()
        mock_send.assert_not_called()  # never dispatched

    async def test_dispatch_failure_refunds_pre_debit(self, test_settings, monkeypatch):
        """When dispatch raises after a successful pre-debit, the charge is refunded."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": "a-1",
                "agent_url": "https://agent.example.com",
                "label": "Test",
                "max_cost_usdc": 50000,
                "require_x402": False,
                "created_at": None,
            }
        )

        mock_card = A2AAgentCard(name="BilledAgent", description="A billed agent")
        config = {"configurable": {"org_id": "org-1", "run_id": "run-1", "db_pool": mock_pool}}

        mock_fund = AsyncMock(return_value=True)
        mock_refund = AsyncMock()

        with (
            patch(f"{_A2A_MOD}.validate_url", return_value=None),
            patch(
                f"{_BILLING_MOD}._get_pool",
                return_value=AsyncMock(
                    fetchrow=AsyncMock(
                        return_value={"balance_usdc": 1_000_000, "spending_limit_usdc": 0, "is_paused": False}
                    )
                ),
            ),
            patch(f"{_A2A_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_A2A_MOD}.send_message", AsyncMock(side_effect=RuntimeError("network down"))),
            patch(f"{_BILLING_MOD}.fund_delegation", mock_fund),
            patch(f"{_BILLING_MOD}.refund_delegation", mock_refund),
            patch(f"{_BILLING_MOD}.record_delegation_event", AsyncMock()),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do work", config=config)

        assert result["status"] == "failed"
        assert result["cost_usdc"] == 0
        mock_fund.assert_called_once()
        mock_refund.assert_called_once()
        # Refund must match the pre-debited amount (50000, fee 0).
        assert mock_refund.call_args.args[1] == 50000

    async def test_incomplete_remote_state_refunds(self, test_settings, monkeypatch):
        """When the remote does not complete, the pre-debit is refunded."""
        import teardrop.config as _config

        monkeypatch.setenv("A2A_DELEGATION_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_BILLING_ENABLED", "true")
        monkeypatch.setenv("A2A_DELEGATION_MAX_COST_USDC", "200000")
        monkeypatch.setenv("A2A_DELEGATION_PLATFORM_FEE_BPS", "0")
        _config.get_settings.cache_clear()

        mock_pool = AsyncMock()
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": "a-1",
                "agent_url": "https://agent.example.com",
                "label": "Test",
                "max_cost_usdc": 50000,
                "require_x402": False,
                "created_at": None,
            }
        )

        mock_card = A2AAgentCard(name="BilledAgent", description="A billed agent")
        mock_response = A2ASendMessageResponse(
            task=A2ATask(id="task-001", status=A2ATaskStatus(state="failed"), artifacts=[]),
            raw={},
        )
        config = {"configurable": {"org_id": "org-1", "run_id": "run-1", "db_pool": mock_pool}}

        mock_fund = AsyncMock(return_value=True)
        mock_refund = AsyncMock()

        with (
            patch(f"{_A2A_MOD}.validate_url", return_value=None),
            patch(
                f"{_BILLING_MOD}._get_pool",
                return_value=AsyncMock(
                    fetchrow=AsyncMock(
                        return_value={"balance_usdc": 1_000_000, "spending_limit_usdc": 0, "is_paused": False}
                    )
                ),
            ),
            patch(f"{_A2A_MOD}.discover_agent_card", AsyncMock(return_value=mock_card)),
            patch(f"{_A2A_MOD}.send_message", AsyncMock(return_value=mock_response)),
            patch(f"{_A2A_MOD}.extract_result_text", return_value=""),
            patch(f"{_BILLING_MOD}.fund_delegation", mock_fund),
            patch(f"{_BILLING_MOD}.refund_delegation", mock_refund),
            patch(f"{_BILLING_MOD}.record_delegation_event", AsyncMock()),
        ):
            result = await delegate_to_agent("https://agent.example.com", "do work", config=config)

        assert result["status"] == "failed"
        assert result["cost_usdc"] == 0
        mock_fund.assert_called_once()
        mock_refund.assert_called_once()
