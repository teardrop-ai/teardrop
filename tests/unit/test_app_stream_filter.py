"""Unit tests for ``_A2UIStreamFilter`` in ``app.py``.

The filter must strip ``\u0060\u0060\u0060a2ui ... \u0060\u0060\u0060`` fences from a streaming
text source while remaining safe against sentinels split arbitrarily across
token boundaries.
"""

from __future__ import annotations

import pytest

from app import _A2UIStreamFilter


def _drain(deltas: list[str]) -> str:
    """Feed deltas through a fresh filter and concatenate the safe output."""
    f = _A2UIStreamFilter()
    parts = [f.feed(d) for d in deltas]
    parts.append(f.flush())
    return "".join(parts)


def test_clean_text_passes_through():
    out = _drain(["Hello, ", "world!", " How are you?"])
    assert out == "Hello, world! How are you?"


def test_single_chunk_with_fence():
    payload = (
        "Here is your dashboard.\n"
        "```a2ui\n{\"components\":[{\"type\":\"Card\"}]}\n```\n"
        "All set!"
    )
    out = _drain([payload])
    # Fence and its enclosed JSON are removed; surrounding prose preserved.
    assert "```" not in out
    assert "a2ui" not in out
    assert "components" not in out
    assert "Here is your dashboard." in out
    assert "All set!" in out


def test_split_open_sentinel():
    # Each character delivered separately — worst case for fence detection.
    full = "before\n```a2ui\n{\"x\":1}\n```\nafter"
    out = _drain(list(full))
    assert "```" not in out
    assert "a2ui" not in out
    assert "{\"x\":1}" not in out
    assert "before" in out
    assert "after" in out


def test_split_close_sentinel():
    # Open in one chunk, JSON in another, close split into single chars.
    deltas = [
        "intro ",
        "```a2ui\n{\"y\":2}\n",
        "`", "`", "`",
        "\noutro",
    ]
    out = _drain(deltas)
    assert "```" not in out
    assert "{\"y\":2}" not in out
    assert "intro " in out
    assert "outro" in out


def test_flush_after_unclosed_fence():
    # Stream ends mid-fence — buffer should be discarded silently.
    deltas = ["safe text ", "```a2ui\n{\"unfinished\":"]
    f = _A2UIStreamFilter()
    parts = [f.feed(d) for d in deltas]
    parts.append(f.flush())
    out = "".join(parts)
    assert out == "safe text "  # text before fence preserved verbatim
    assert "```" not in out
    assert "a2ui" not in out
    assert "unfinished" not in out


def test_text_after_close_fence():
    # Make sure text immediately following the close fence is emitted.
    out = _drain(["```a2ui\n{}\n```tail"])
    assert out == "tail"


def test_no_fence_no_buffer_growth():
    # Without a fence, output should equal input (modulo small lookahead held
    # in the buffer until flush).
    f = _A2UIStreamFilter()
    chunks = ["abc", "def", "ghi"]
    streamed = "".join(f.feed(c) for c in chunks)
    final = f.flush()
    assert streamed + final == "abcdefghi"


def test_fence_at_start_of_stream():
    out = _drain(["```a2ui\n{\"a\":1}\n```\nhello"])
    assert out == "hello"


def test_multiple_fences_in_stream():
    # Pathological: two fences back-to-back. Filter should strip both.
    payload = (
        "A "
        "```a2ui\n{\"1\":1}\n``` "
        "B "
        "```a2ui\n{\"2\":2}\n``` "
        "C"
    )
    out = _drain([payload])
    assert "```" not in out
    assert "{\"1\":1}" not in out
    assert "{\"2\":2}" not in out
    assert "A " in out and " B " in out and " C" in out


def test_partial_open_then_not_a_fence():
    # If we see ``` but it's not followed by a2ui, it must NOT be suppressed.
    out = _drain(["look at this code: ```python\nprint('hi')\n``` done"])
    assert "```python" in out
    assert "print('hi')" in out
    assert "``` done" in out


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
