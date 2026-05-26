from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.judge import JudgeResult, llm_judge, score_task_async
from evals.runner import EvalTask, RunArtifact, run_suite


@pytest.mark.asyncio
async def test_llm_judge_parses_and_clamps_score(monkeypatch):
    class FakeJudge:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "real-key"

        async def ainvoke(self, messages):
            assert "Rubric:" in messages[0].content
            return SimpleNamespace(content='{"score": 1.7, "reasoning": "strong answer"}')

    monkeypatch.setattr("evals.judge.ChatAnthropic", FakeJudge)

    result = await llm_judge("Answer clearly", "Here is the answer", api_key="real-key")

    assert result == JudgeResult(score=1.0, reasoning="strong answer")


@pytest.mark.asyncio
async def test_score_task_async_falls_back_for_test_api_key():
    score = await score_task_async(
        scorer="llm_judge",
        rubric="Mention USDC and Base",
        expected_text_contains=["USDC", "Base"],
        actual_text="USDC is on Base",
        api_key="test-dummy-key",
    )

    assert score == 1.0


@pytest.mark.asyncio
async def test_score_task_async_falls_back_when_judge_returns_invalid_json(monkeypatch):
    class FakeJudge:
        def __init__(self, **kwargs):
            pass

        async def ainvoke(self, messages):
            return SimpleNamespace(content="not-json")

    monkeypatch.setattr("evals.judge.ChatAnthropic", FakeJudge)

    score = await score_task_async(
        scorer="llm_judge",
        rubric="Mention USDC and Base",
        expected_text_contains=["USDC", "Base"],
        actual_text="USDC is on Base",
        api_key="real-key",
    )

    assert score == 1.0


@pytest.mark.asyncio
async def test_score_task_async_returns_zero_without_deterministic_fallback():
    score = await score_task_async(
        scorer="llm_judge",
        rubric="Answer clearly",
        expected_text_contains=[],
        expected_text_not_contains=[],
        actual_text="Any answer",
        api_key="test-dummy-key",
    )

    assert score == 0.0


@pytest.mark.asyncio
async def test_score_task_async_preserves_non_judge_scorers():
    score = await score_task_async(
        scorer="contains",
        rubric="unused",
        expected_text_contains=["ETH", "USD"],
        actual_text="ETH price in USD is 3000",
        api_key="",
    )

    assert score == 1.0


@pytest.mark.asyncio
async def test_run_suite_passes_judge_config(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_score_task_async(**kwargs):
        calls.append(kwargs)
        return 1.0

    monkeypatch.setattr("evals.runner.score_task_async", fake_score_task_async)
    task = EvalTask(
        id="judge.example.001",
        messages=[{"role": "user", "content": "hello"}],
        scorer="llm_judge",
        rubric="Mention hello",
    )

    async def fake_runner(_task):
        return RunArtifact(text="hello", duration_ms=10)

    report = await run_suite(
        suite_name="judge",
        tasks=[task],
        run_task=fake_runner,
        judge_api_key="real-key",
        judge_model="judge-model",
    )

    assert report.passed_tasks == 1
    assert calls == [
        {
            "scorer": "llm_judge",
            "rubric": "Mention hello",
            "expected_text_contains": [],
            "expected_text_not_contains": [],
            "actual_text": "hello",
            "api_key": "real-key",
            "judge_model": "judge-model",
        }
    ]
