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
    description=("Return word count, character count, sentence count, and paragraph statistics for a given text."),
    tags=["text", "analysis", "statistics"],
    input_schema=SummarizeTextInput,
    output_schema=SummarizeTextOutput,
    implementation=count_text_stats,
)
