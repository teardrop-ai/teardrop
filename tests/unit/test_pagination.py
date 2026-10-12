# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for keyset pagination cursors."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from shared.pagination import encode_keyset_cursor, parse_keyset_cursor


def test_keyset_cursor_round_trips_microsecond_timestamp_and_id():
    created_at = datetime(2026, 10, 12, 4, 23, 24, 29490, tzinfo=timezone.utc)

    assert parse_keyset_cursor(encode_keyset_cursor(created_at, "result-9")) == (created_at, "result-9")


def test_legacy_iso_cursor_has_no_tiebreaker():
    assert parse_keyset_cursor("2026-10-12T04:23:24.029490+00:00") == (
        datetime(2026, 10, 12, 4, 23, 24, 29490, tzinfo=timezone.utc),
        None,
    )


def _token(data) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        _token({"created_at": "2026-10-12T00:00:00+00:00"}),
        _token({"created_at": "2026-10-12T00:00:00+00:00", "id": ""}),
        _token({"created_at": "yesterday", "id": "result-1"}),
        _token(["2026-10-12T00:00:00+00:00", "result-1"]),
    ],
)
def test_malformed_cursor_returns_400(cursor):
    with pytest.raises(HTTPException) as exc:
        parse_keyset_cursor(cursor)

    assert exc.value.status_code == 400
