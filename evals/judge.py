# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Async judge helpers for eval tasks."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from evals.scorer import score_task

try:
    from langchain_anthropic import ChatAnthropic
except ImportError:
    ChatAnthropic = None  # type: ignore[assignment,misc]

try:
    from langchain_core.messages import HumanMessage
except ImportError:
    HumanMessage = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"


class JudgeError(RuntimeError):
    """Raised when the LLM judge cannot return a usable score."""


class JudgeResult(BaseModel):
    score: float
    reasoning: str = ""


def _fallback_score(
    *,
    expected_text_contains: list[str],
    expected_text_not_contains: list[str] | None,
    actual_text: str,
) -> float:
    if not expected_text_contains and not (expected_text_not_contains or []):
        return 0.0
    return score_task(
        scorer="contains",
        expected_text_contains=expected_text_contains,
        expected_text_not_contains=expected_text_not_contains,
        actual_text=actual_text,
    )


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _extract_json_object(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise JudgeError("Judge response did not contain a JSON object")
    return raw[start : end + 1]


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _build_prompt(rubric: str, actual_text: str) -> str:
    return (
        "You are grading an agent response against a rubric. "
        "Return only valid JSON with keys score and reasoning. "
        "Score must be a number between 0.0 and 1.0.\n\n"
        f"Rubric:\n{rubric}\n\n"
        "Response:\n<response>\n"
        f"{actual_text}\n"
        "</response>"
    )


async def llm_judge(
    rubric: str,
    actual_text: str,
    *,
    api_key: str,
    model: str = _DEFAULT_JUDGE_MODEL,
) -> JudgeResult:
    if ChatAnthropic is None or HumanMessage is None:
        raise JudgeError("langchain-anthropic is not installed")

    try:
        judge = ChatAnthropic(
            model=model,
            api_key=api_key,
            temperature=0.0,
            max_tokens=256,
        )
        response = await judge.ainvoke([HumanMessage(content=_build_prompt(rubric, actual_text))])
        payload = json.loads(_extract_json_object(_message_text(getattr(response, "content", ""))))
        result = JudgeResult.model_validate(payload)
    except JudgeError:
        raise
    except Exception as exc:
        raise JudgeError("Judge request failed") from exc

    return JudgeResult(score=_clamp_score(float(result.score)), reasoning=result.reasoning)


async def score_task_async(
    *,
    scorer: str,
    rubric: str,
    expected_text_contains: list[str],
    actual_text: str,
    expected_text_not_contains: list[str] | None = None,
    api_key: str = "",
    judge_model: str = _DEFAULT_JUDGE_MODEL,
) -> float:
    if scorer != "llm_judge":
        return score_task(
            scorer=scorer,
            expected_text_contains=expected_text_contains,
            expected_text_not_contains=expected_text_not_contains,
            actual_text=actual_text,
        )

    if not rubric.strip():
        logger.warning("LLM judge requested without rubric; using contains fallback")
        return _fallback_score(
            expected_text_contains=expected_text_contains,
            expected_text_not_contains=expected_text_not_contains,
            actual_text=actual_text,
        )

    if not api_key or api_key.startswith("test-"):
        logger.warning("LLM judge disabled due to missing non-test API key; using contains fallback")
        return _fallback_score(
            expected_text_contains=expected_text_contains,
            expected_text_not_contains=expected_text_not_contains,
            actual_text=actual_text,
        )

    try:
        result = await llm_judge(rubric, actual_text, api_key=api_key, model=judge_model)
        return result.score
    except JudgeError as exc:
        logger.warning("LLM judge failed; using contains fallback: %s", exc)
        return _fallback_score(
            expected_text_contains=expected_text_contains,
            expected_text_not_contains=expected_text_not_contains,
            actual_text=actual_text,
        )
