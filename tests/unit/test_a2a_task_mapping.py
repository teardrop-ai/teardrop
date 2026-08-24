# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import pytest

from scheduling.a2a_tasks import to_a2a_task_state


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "TASK_STATE_SUBMITTED"),
        ("completed", "TASK_STATE_COMPLETED"),
        ("failed", "TASK_STATE_FAILED"),
        ("timeout", "TASK_STATE_FAILED"),
        ("skipped", "TASK_STATE_REJECTED"),
        ("unexpected", "TASK_STATE_FAILED"),
    ],
)
def test_to_a2a_task_state(status: str | None, expected: str) -> None:
    assert to_a2a_task_state(status) == expected
