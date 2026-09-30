# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Focused tests for shared non-financial post-run telemetry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from teardrop.agent_post_run import record_post_run_telemetry


@pytest.mark.anyio
async def test_record_post_run_telemetry_schedules_tool_and_memory_records():
    from teardrop import agent_post_run

    tool_events = AsyncMock()
    memory_extraction = AsyncMock()
    scheduled: list[object] = []

    def capture_task(coroutine, *, name):
        scheduled.append(coroutine)
        return True

    with (
        patch.object(agent_post_run, "record_tool_call_events", tool_events),
        patch.object(agent_post_run, "extract_and_store_memories", memory_extraction),
        patch.object(agent_post_run, "schedule_telemetry_task", capture_task),
    ):
        record_post_run_telemetry(
            run_id="run-1",
            org_id="org-1",
            user_id="user-1",
            usage_data={
                "_tool_call_log": [{"tool_name": "platform/weather", "args_hash": "safe-hash"}],
                "billable_tool_names": ["platform/weather"],
            },
            state_values={"messages": ["older"] * 11, "slots": {"quotes": {"ETH": "safe"}}},
            settings=SimpleNamespace(tool_call_event_logging_enabled=True, memory_enabled=True),
            outcome=1,
            outcome_source="auto",
            source="schedule",
        )
        await scheduled[0]
        await scheduled[1]

    tool_events.assert_awaited_once_with(
        "run-1",
        "org-1",
        [{"tool_name": "platform/weather", "args_hash": "safe-hash"}],
        source="schedule",
    )
    memory_extraction.assert_awaited_once_with(
        "org-1",
        "user-1",
        ["older"] * 10,
        "run-1",
        tool_names_used=["platform/weather"],
        slots={"quotes": {"ETH": "safe"}},
        outcome=1,
        outcome_source="auto",
        thread_id="",
        user_message="",
        source="schedule",
    )


@pytest.mark.anyio
async def test_record_post_run_telemetry_skips_disabled_or_missing_state():
    from teardrop import agent_post_run

    with (
        patch.object(agent_post_run, "record_tool_call_events", AsyncMock()) as tool_events,
        patch.object(agent_post_run, "extract_and_store_memories", AsyncMock()) as memory_extraction,
    ):
        record_post_run_telemetry(
            run_id="run-1",
            org_id="org-1",
            user_id="user-1",
            usage_data={"_tool_call_log": [{"tool_name": "weather"}]},
            state_values=None,
            settings=SimpleNamespace(tool_call_event_logging_enabled=False, memory_enabled=True),
        )

    tool_events.assert_not_awaited()
    memory_extraction.assert_not_awaited()


_USAGE = {
    "tokens_in": 4_000,
    "tokens_out": 1_000,
    "billable_tool_calls": 2,
    "billable_tool_names": ["acme/weather", "calculate"],
    "tool_names": ["acme/weather", "calculate", "failed_tool"],
}


@pytest.mark.anyio
async def test_calculate_run_cost_prices_tools_once_independent_of_turn_models():
    from teardrop import agent_post_run

    token_cost = AsyncMock(return_value=100)
    tool_cost = AsyncMock(return_value=3_500)
    usage = {
        **_USAGE,
        "turns": [
            {"provider": "openrouter", "model": "deepseek/x", "tokens_in": 4_000, "tokens_out": 1_000},
            {"tokens_in": 1_000, "tokens_out": 0},
            "not-a-turn",
        ],
    }
    with (
        patch.object(agent_post_run, "calculate_turns_token_cost_usdc", token_cost),
        patch.object(agent_post_run, "calculate_tool_cost_usdc", tool_cost),
    ):
        cost = await agent_post_run.calculate_run_cost(
            usage_data=usage, llm_config=None, settings=SimpleNamespace(agent_provider="anthropic", agent_model="claude-x")
        )

    assert cost == 3_600
    tool_cost.assert_awaited_once_with(2, ["acme/weather", "calculate"])
    token_cost.assert_awaited_once_with(
        [
            {"provider": "openrouter", "model": "deepseek/x", "tokens_in": 4_000, "tokens_out": 1_000},
            {"provider": "anthropic", "model": "claude-x", "tokens_in": 1_000, "tokens_out": 0},
        ]
    )


async def _drain(gen) -> None:
    async for _ in gen:
        pass


@pytest.mark.anyio
@pytest.mark.parametrize(("tier_enabled", "expected_fee"), [(False, 1_000), (True, 1_700)])
async def test_byok_run_cost_is_orchestration_fee_plus_tools(tier_enabled, expected_fee):
    from teardrop import agent_post_run

    token_cost = AsyncMock(return_value=90_000)
    tool_cost = AsyncMock(return_value=3_500)
    with (
        patch.object(agent_post_run, "calculate_run_cost_usdc", token_cost),
        patch.object(agent_post_run, "calculate_tool_cost_usdc", tool_cost),
        patch.object(agent_post_run, "calculate_byok_orchestration_cost", AsyncMock(return_value=1_700)),
    ):
        cost = await agent_post_run.calculate_run_cost(
            usage_data=_USAGE,
            llm_config=None,
            settings=SimpleNamespace(byok_tier_pricing_enabled=tier_enabled),
            is_byok=True,
            org_llm_cfg=SimpleNamespace(provider="openai", model="gpt-x"),
            platform_fee=1_000,
        )

    assert cost == expected_fee + 3_500
    tool_cost.assert_awaited_once_with(2, ["acme/weather", "calculate"])
    token_cost.assert_not_awaited()


@pytest.mark.anyio
async def test_x402_settles_the_run_charge():
    from teardrop import agent_post_run

    settle = AsyncMock(return_value=SimpleNamespace(settled=False, amount_usdc=0, tx_hash="", error="x"))
    with (
        patch.object(agent_post_run, "settle_payment", settle),
        patch.object(agent_post_run, "record_settlement", AsyncMock()),
        patch.object(agent_post_run, "enqueue_failed_settlement", AsyncMock()) as enqueue,
    ):
        await _drain(
            agent_post_run.dispatch_settlement(
                billing=SimpleNamespace(verified=True, billing_method="x402", payment_payload=None),
                settings=SimpleNamespace(
                    x402_scheme="upto",
                    x402_upto_max_amount_atomic=0,
                    x402_settlement_timeout_seconds=5,
                ),
                usage_event=SimpleNamespace(id="ue-1"),
                platform_fee=1_000,
                cost_usdc=4_500,
                delegation_spend=0,
                org_id="org-1",
                principal_id="user-1",
                run_id="run-1",
                result={},
            )
        )

    assert settle.await_args.kwargs["actual_cost_usdc"] == 4_500
    assert enqueue.await_args.args[4] == 4_500


@pytest.mark.anyio
async def test_x402_failure_skips_retry_when_result_is_withheld():
    from teardrop import agent_post_run

    settle = AsyncMock(return_value=SimpleNamespace(settled=False, amount_usdc=0, tx_hash="", error="x"))
    result: dict = {}
    with (
        patch.object(agent_post_run, "settle_payment", settle),
        patch.object(agent_post_run, "record_settlement", AsyncMock()) as record,
        patch.object(agent_post_run, "enqueue_failed_settlement", AsyncMock()) as enqueue,
    ):
        await _drain(
            agent_post_run.dispatch_settlement(
                billing=SimpleNamespace(verified=True, billing_method="x402", payment_payload=None),
                settings=SimpleNamespace(x402_scheme="exact", x402_settlement_timeout_seconds=5),
                usage_event=SimpleNamespace(id="ue-1"),
                platform_fee=0,
                cost_usdc=10_000,
                delegation_spend=0,
                org_id="",
                principal_id="",
                run_id="run-1",
                result=result,
                enqueue_x402_retry=False,
            )
        )

    record.assert_awaited_once_with("ue-1", 0, "", "failed")
    enqueue.assert_not_awaited()
    assert result["settlement_tx"] == ""


def _dispatch(billing, *, source: str = "api", cost_usdc: int = 4_000, org_id: str = "org-1"):
    from teardrop import agent_post_run

    return agent_post_run.dispatch_settlement(
        billing=billing,
        settings=SimpleNamespace(x402_scheme="exact", x402_settlement_timeout_seconds=5, x402_network="eip155:8453"),
        usage_event=SimpleNamespace(id="ue-1"),
        platform_fee=0,
        cost_usdc=cost_usdc,
        delegation_spend=0,
        org_id=org_id,
        principal_id="user-1",
        run_id="run-1",
        result={},
        source=source,
    )


@pytest.mark.anyio
async def test_credit_success_records_settled_charge():
    from teardrop import agent_post_run

    with (
        patch.object(agent_post_run, "debit_credit", AsyncMock(return_value=(True, 3_900))),
        patch.object(agent_post_run, "record_settlement", AsyncMock()),
        patch.object(agent_post_run, "record_charge", AsyncMock(return_value="charge-1")) as charge,
    ):
        await _drain(_dispatch(SimpleNamespace(verified=True, billing_method="credit", payer=""), source="schedule"))

    kwargs = charge.await_args.kwargs
    assert kwargs["source"] == "schedule"
    assert kwargs["invocation_id"] == "run-1"
    assert kwargs["usage_event_id"] == "ue-1"
    assert (kwargs["status"], kwargs["amount_usdc"], kwargs["settled_amount_usdc"]) == ("settled", 4_000, 3_900)


@pytest.mark.anyio
async def test_credit_failure_links_retry_to_failed_charge():
    from teardrop import agent_post_run

    with (
        patch.object(agent_post_run, "debit_credit", AsyncMock(return_value=(False, 0))),
        patch.object(agent_post_run, "record_settlement", AsyncMock()),
        patch.object(agent_post_run, "record_charge", AsyncMock(return_value="charge-1")) as charge,
        patch.object(agent_post_run, "enqueue_failed_settlement", AsyncMock()) as enqueue,
    ):
        await _drain(_dispatch(SimpleNamespace(verified=True, billing_method="credit", payer="")))

    assert charge.await_args.kwargs["status"] == "failed"
    assert enqueue.await_args.kwargs == {"principal_id": "user-1", "charge_id": "charge-1"}


@pytest.mark.anyio
async def test_x402_success_records_payer_and_tx_on_charge():
    from teardrop import agent_post_run

    settled = SimpleNamespace(settled=True, amount_usdc=4_000, tx_hash="0xtx", error="")
    with (
        patch.object(agent_post_run, "settle_payment", AsyncMock(return_value=settled)),
        patch.object(agent_post_run, "record_settlement", AsyncMock()),
        patch.object(agent_post_run, "record_charge", AsyncMock(return_value="charge-1")) as charge,
        patch.object(agent_post_run, "verify_settlement_on_chain", AsyncMock()),
    ):
        billing = SimpleNamespace(verified=True, billing_method="x402", payer="0xPayer", payment_payload=None)
        await _drain(_dispatch(billing, source="a2a", org_id=""))

    kwargs = charge.await_args.kwargs
    assert kwargs["source"] == "a2a"
    assert kwargs["payer_address"] == "0xPayer"
    assert (kwargs["status"], kwargs["settlement_tx"], kwargs["org_id"]) == ("settled", "0xtx", "")
