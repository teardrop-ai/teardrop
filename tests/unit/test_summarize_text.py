"""Unit tests for tools/definitions/summarize_text.py."""

from __future__ import annotations

import pytest

from tools.definitions.summarize_text import summarize_text


@pytest.mark.anyio
async def test_basic_output_shape():
    result = await summarize_text("Hello world. Goodbye world.")
    assert "character_count" in result
    assert "word_count" in result
    assert "sentence_count" in result
    assert "paragraph_count" in result
    assert "average_words_per_sentence" in result


@pytest.mark.anyio
async def test_word_count():
    result = await summarize_text("one two three four five")
    assert result["word_count"] == 5


@pytest.mark.anyio
async def test_sentence_count():
    result = await summarize_text("First sentence. Second sentence! Third?")
    assert result["sentence_count"] == 3


@pytest.mark.anyio
async def test_paragraph_count():
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    result = await summarize_text(text)
    assert result["paragraph_count"] == 3


@pytest.mark.anyio
async def test_character_count():
    text = "Hello"
    result = await summarize_text(text)
    assert result["character_count"] == 5


@pytest.mark.anyio
async def test_average_words_per_sentence():
    # 4 words, 2 sentences → average 2.0
    result = await summarize_text("One two. Three four.")
    assert result["average_words_per_sentence"] == pytest.approx(2.0)


@pytest.mark.anyio
async def test_empty_string_does_not_raise():
    result = await summarize_text("")
    assert result["word_count"] == 0
    assert result["sentence_count"] == 0
    assert result["average_words_per_sentence"] == 0.0


@pytest.mark.anyio
async def test_single_paragraph_no_double_newline():
    result = await summarize_text("Just one paragraph here.")
    assert result["paragraph_count"] == 1
