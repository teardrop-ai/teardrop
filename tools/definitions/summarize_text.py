# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Summarize-text tool – word, sentence, and paragraph statistics."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from tools.registry import ToolDefinition

# ─── Schemas ──────────────────────────────────────────────────────────────────


class SummarizeTextInput(BaseModel):
    text: str = Field(..., description="Text to summarize", max_length=10_000)


class SummarizeTextOutput(BaseModel):
    character_count: int
    word_count: int
    sentence_count: int
    paragraph_count: int
    average_words_per_sentence: float


# ─── Implementation ──────────────────────────────────────────────────────────


async def count_text_stats(text: str) -> dict[str, Any]:
    """Return basic statistics about the provided text."""
    words = text.split()
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return {
        "character_count": len(text),
        "word_count": len(words),
        "sentence_count": len(sentences),
        "paragraph_count": len(paragraphs),
        "average_words_per_sentence": round(len(words) / max(len(sentences), 1), 1),
    }


# ─── Tool definition ─────────────────────────────────────────────────────────

TOOL = ToolDefinition(
    name="count_text_stats",
    version="1.0.0",
    description=(
        "Return word, character, sentence, and paragraph counts plus average words per sentence for a "
        "given text, for length checks and content sizing decisions."
    ),
    tags=["text", "analysis", "statistics"],
    use_when=(
        "Use when output length must be validated or reported — e.g. verifying a summary meets a length "
        "contract or comparing document sizes. Do not use for semantic summarization; it counts only."
    ),
    limitations=(
        "Pure counting on the provided text — no language detection, no reading-level or sentiment "
        "analysis, and no retrieval of external content."
    ),
    alternatives=["web_search", "http_fetch"],
    input_schema=SummarizeTextInput,
    output_schema=SummarizeTextOutput,
    show_on_agent_card=False,
    implementation=count_text_stats,
)
