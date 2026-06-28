# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Background worker entrypoints for unattended scheduled runs."""

from __future__ import annotations

import asyncio
import logging

from scheduling.crud import claim_due_schedules
from scheduling.models import ScheduledRun
from scheduling.runner import execute_scheduled_run
from teardrop.config import get_settings

logger = logging.getLogger(__name__)

_CLAIM_LIMIT = 25


async def _execute_isolated(schedule: ScheduledRun, semaphore: asyncio.Semaphore) -> None:
    """Run one schedule under the concurrency cap, isolating failures.

    A raised exception from a single execution must not abort the rest of the
    claimed batch (those rows already had ``next_run_at`` advanced at claim
    time, so dropping them would skip the interval). Cancellation propagates.
    """
    async with semaphore:
        try:
            await execute_scheduled_run(schedule)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled run execution failed schedule_id=%s", schedule.id)


async def scheduled_runs_tick() -> None:
    schedules = await claim_due_schedules(_CLAIM_LIMIT)
    if not schedules:
        return
    concurrency = max(1, get_settings().scheduled_runs_max_concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    logger.info("scheduled_runs_tick claimed=%d concurrency=%d", len(schedules), concurrency)
    await asyncio.gather(*(_execute_isolated(schedule, semaphore) for schedule in schedules))
