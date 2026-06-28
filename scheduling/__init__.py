# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Scheduled run package facade."""

from scheduling.context import close_scheduling_db, init_scheduling_db
from scheduling.crud import (
    count_scheduled_runs,
    create_event_trigger,
    create_scheduled_run,
    delete_scheduled_run,
    get_event_trigger_for_dispatch,
    get_existing_dispatch,
    get_scheduled_run,
    list_event_triggers,
    list_scheduled_run_results,
    list_scheduled_runs,
    reserve_event_dispatch,
    rotate_event_trigger_secret,
    update_scheduled_run,
)
from scheduling.models import ScheduledRun, ScheduledRunResult
from scheduling.runner import execute_event_run, execute_scheduled_run
from scheduling.templating import render_event_prompt
from scheduling.worker import scheduled_runs_tick

__all__ = [
    "ScheduledRun",
    "ScheduledRunResult",
    "close_scheduling_db",
    "count_scheduled_runs",
    "create_event_trigger",
    "create_scheduled_run",
    "delete_scheduled_run",
    "execute_event_run",
    "execute_scheduled_run",
    "get_event_trigger_for_dispatch",
    "get_existing_dispatch",
    "get_scheduled_run",
    "init_scheduling_db",
    "list_event_triggers",
    "list_scheduled_run_results",
    "list_scheduled_runs",
    "render_event_prompt",
    "reserve_event_dispatch",
    "rotate_event_trigger_secret",
    "scheduled_runs_tick",
    "update_scheduled_run",
]
