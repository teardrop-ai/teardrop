# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared pagination helpers."""

from __future__ import annotations

import base64
import binascii
import json
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


KeysetCursor = tuple[datetime, str | None]


def encode_keyset_cursor(created_at: datetime, row_id: str) -> str:
    """Encode an opaque ``(created_at, id)`` cursor so rows sharing a timestamp are not skipped."""
    payload = json.dumps({"created_at": created_at.isoformat(), "id": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def parse_keyset_cursor(cursor: str | None) -> KeysetCursor | None:
    """Parse an ``encode_keyset_cursor`` token or a legacy ISO 8601 timestamp.

    Legacy timestamps return ``(created_at, None)`` and keep timestamp-only semantics.
    Raises HTTP 400 on malformed values.
    """
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor), None
    except ValueError:
        pass
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        created_at, row_id = data["created_at"], data["id"]
        if not isinstance(created_at, str) or not isinstance(row_id, str) or not row_id:
            raise ValueError
        return datetime.fromisoformat(created_at), row_id
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor format — use next_cursor from a previous response.",
        )
