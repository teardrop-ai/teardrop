# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared pagination helpers."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status


def parse_cursor(cursor: str | None) -> datetime | None:
    """Parse an ISO 8601 cursor string to a ``datetime``.

    Returns ``None`` when *cursor* is empty/None. Raises HTTP 400 on a
    malformed value so endpoints can rely on a single, consistent error
    response shape across the API.
    """
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor format — must be an ISO 8601 timestamp.",
        )
