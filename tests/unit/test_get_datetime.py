"""Unit tests for tools/definitions/get_datetime.py."""

from __future__ import annotations

import pytest

from tools.definitions.get_datetime import get_datetime


@pytest.mark.anyio
async def test_output_has_required_keys():
    result = await get_datetime()
    assert "datetime" in result
    assert "iso8601" in result


@pytest.mark.anyio
async def test_iso8601_ends_with_utc_offset():
    result = await get_datetime()
    assert result["iso8601"].endswith("+00:00")


@pytest.mark.anyio
async def test_default_format_is_human_readable():
    result = await get_datetime()
    # Default format: "%Y-%m-%d %H:%M:%S UTC"
    assert "UTC" in result["datetime"]


@pytest.mark.anyio
async def test_custom_format():
    result = await get_datetime(format="%Y")
    # Should be a 4-digit year
    assert len(result["datetime"]) == 4
    assert result["datetime"].isdigit()


@pytest.mark.anyio
async def test_bad_format_falls_back_gracefully():
    # A completely invalid strftime format should still return something
    result = await get_datetime(format="%~INVALID~%")
    assert "datetime" in result
    assert result["datetime"] is not None
