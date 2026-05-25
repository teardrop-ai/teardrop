# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Shared helpers for immutable audit-event inserts."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg


async def insert_event_row(
    pool: asyncpg.Pool,
    *,
    insert_sql: str,
    values: tuple[Any, ...],
) -> None:
    """Execute an audit insert where the first placeholder is an event id."""
    await pool.execute(insert_sql, str(uuid.uuid4()), *values)
