from __future__ import annotations

import pytest

from agent.planner_ir import Plan, parse_plan_from_text, resolve_plan_references, validate_plan_dag


def test_parse_plan_from_text_round_trip():
    text = (
        "some content\n"
        '<plan>{"stages":[{"stage_id":1,"calls":[{"call_id":"c1","tool":"resolve_ens","args":{},"depends_on":[]}]}],"synthesizer_after_stage":1}</plan>'
    )
    plan = parse_plan_from_text(text)
    assert plan is not None
    assert len(plan.stages) == 1
    assert plan.stages[0].calls[0].tool == "resolve_ens"


def test_validate_plan_dag_rejects_cycle():
    plan = Plan.model_validate(
        {
            "stages": [
                {
                    "stage_id": 1,
                    "calls": [
                        {"call_id": "a", "tool": "x", "args": {}, "depends_on": ["b"]},
                        {"call_id": "b", "tool": "y", "args": {}, "depends_on": ["a"]},
                    ],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_plan_dag(plan)


def test_resolve_plan_references_nested():
    outputs = {"c1": {"address": "0xabc", "token": {"symbol": "USDC"}}}
    args = {"wallet": "{{c1.address}}", "symbol": "{{c1.token.symbol}}"}
    resolved = resolve_plan_references(args, outputs)
    assert resolved["wallet"] == "0xabc"
    assert resolved["symbol"] == "USDC"
