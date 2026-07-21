"""Unit tests for privacy-preserving production eval candidate promotion."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from evals import cli


@pytest.mark.anyio
async def test_promotion_query_returns_sanitized_human_positive_metadata():
    connection = MagicMock()
    connection.fetch = AsyncMock(
        return_value=[
            {
                "run_id": "run-1",
                "source": "a2a",
                "task_class": "portfolio_lookup",
                "tool_names": ["platform/get_wallet_portfolio", "not a safe tool name!"],
                "outcome_source": "explicit",
                "confidence": 0.9,
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "runner_version": "1.4.0",
                "duration_ms": 1200,
                "cost_usdc": 15000,
                "created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
            }
        ]
    )

    candidates = await cli._query_promotion_candidates(connection, org_id="org-1", lookback_days=30, limit=20)

    assert candidates == [
        cli.EvalPromotionCandidate(
            run_id="run-1",
            source="a2a",
            task_class="portfolio_lookup",
            tool_names=["platform/get_wallet_portfolio"],
            outcome_source="explicit",
            confidence=0.9,
            provider="anthropic",
            model="claude-sonnet-4-6",
            runner_version="1.4.0",
            duration_ms=1200,
            cost_usdc=15000,
            created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
    ]
    query, lookback_days, limit, org_id = connection.fetch.await_args.args
    assert (lookback_days, limit, org_id) == (30, 20, "org-1")
    assert "u.org_id = d.org_id" in query
    assert "d.org_id = $3" in query
    for forbidden_column in ("reasoning", "slots_snapshot", "user_id", "messages", "output_text", "args_hash"):
        assert forbidden_column not in query
    assert "org_id" not in candidates[0].model_dump()


def test_promotion_queue_is_json_review_metadata_not_a_runnable_eval_task():
    rendered = cli._render_promotion_queue(
        [
            cli.EvalPromotionCandidate(
                run_id="run-1",
                outcome_source="feedback",
                created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            )
        ]
    )

    payload = json.loads(rendered)
    assert payload["schema_version"] == 1
    assert payload["review_required"] is True
    assert payload["candidates"][0]["run_id"] == "run-1"
    assert "messages" not in payload["candidates"][0]
    assert "expected_tool_calls" not in payload["candidates"][0]


@pytest.mark.anyio
async def test_promote_requires_a_database_dsn(capsys):
    exit_code = await cli._run_promotion(SimpleNamespace(pg_dsn=""))

    assert exit_code == 2
    assert "requires --pg-dsn" in capsys.readouterr().err


def test_legacy_eval_invocation_and_promote_parser_remain_separate():
    parser = cli._build_promotion_parser()
    args = parser.parse_args(["--org-id", "org-1", "--lookback-days", "14", "--limit", "3"])

    assert args.org_id == "org-1"
    assert args.lookback_days == 14
    assert args.limit == 3
    assert cli._resolve_suite_path("smoke").name == "smoke.yaml"


def test_promote_parser_requires_a_nonempty_organization_id():
    parser = cli._build_promotion_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--org-id", "   "])
