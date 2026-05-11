from __future__ import annotations

from evals.scorer import score_contains_pattern, score_not_contains, score_task


def test_score_contains_pattern_matches_all_patterns():
    text = "Aave supply APY is 3.45% and Compound is 2.10%"
    score = score_contains_pattern([r"\d+\.\d+%", r"Aave", r"Compound"], text)
    assert score == 1.0


def test_score_contains_pattern_partial_match():
    text = "Current TVL is $123,456"
    score = score_contains_pattern([r"\$\d", r"Aave"], text)
    assert score == 0.5


def test_score_task_contains_pattern_branch():
    text = "Borrow APY: 4.12%"
    score = score_task(
        scorer="contains_pattern",
        expected_text_contains=[r"\d+\.\d+%"],
        actual_text=text,
    )
    assert score == 1.0


def test_score_not_contains_scores_zero_when_excluded_phrase_present():
    text = "Top APY is up to 14.5% this week"
    score = score_not_contains(["up to 14.5%"], text)
    assert score == 0.0


def test_score_task_applies_negative_filter():
    text = "USDC yield is 5.1% based on 30d mean"
    score = score_task(
        scorer="contains",
        expected_text_contains=["USDC", "30d"],
        expected_text_not_contains=["7-day trailing"],
        actual_text=text,
    )
    assert score == 1.0


def test_score_task_not_contains_mode():
    text = "No short-term spikes reported"
    score = score_task(
        scorer="not_contains",
        expected_text_contains=[],
        expected_text_not_contains=["up to", "7-day trailing"],
        actual_text=text,
    )
    assert score == 1.0
