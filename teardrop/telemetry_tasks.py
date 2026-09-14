# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Bounded lifecycle management for droppable post-run telemetry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_max_tasks = 32
_tasks: set[asyncio.Task[None]] = set()


def init_telemetry_tasks(max_tasks: int) -> None:
    """Set the process-local task cap during application startup."""
    if max_tasks < 1:
        raise ValueError("Post-run telemetry task limit must be positive")
    global _max_tasks
    _max_tasks = max_tasks


def schedule_telemetry_task(coroutine: Coroutine[Any, Any, None], *, name: str) -> bool:
    """Schedule best-effort telemetry unless the bounded registry is full."""
    if len(_tasks) >= _max_tasks:
        coroutine.close()
        logger.warning("Dropping post-run telemetry task because capacity is exhausted: name=%s", name)
        return False

    started = False

    async def _run() -> None:
        nonlocal started
        started = True
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Post-run telemetry task failed: name=%s", name, exc_info=True)

    task = asyncio.create_task(_run())
    _tasks.add(task)

    def _discard(completed: asyncio.Task[None]) -> None:
        _tasks.discard(completed)
        if completed.cancelled() and not started:
            coroutine.close()

    task.add_done_callback(_discard)
    return True


async def close_telemetry_tasks() -> None:
    """Cancel tracked telemetry before its database and provider clients close."""
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
