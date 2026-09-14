# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

import pytest

from teardrop.concurrency import (
    AgentRunCapacityError,
    acquire_agent_run_slot,
    init_agent_run_limiter,
    try_acquire_agent_run_slot,
)


@pytest.fixture(autouse=True)
def reset_limiter():
    yield
    init_agent_run_limiter(16)


def test_limiter_rejects_saturation_and_releases_once():
    init_agent_run_limiter(1)
    lease = try_acquire_agent_run_slot()
    assert lease is not None
    assert try_acquire_agent_run_slot() is None

    lease.release()
    lease.release()
    replacement = try_acquire_agent_run_slot()
    assert replacement is not None
    replacement.release()


@pytest.mark.anyio
async def test_context_manager_raises_without_waiting_when_saturated():
    init_agent_run_limiter(1)
    lease = try_acquire_agent_run_slot()
    assert lease is not None
    try:
        with pytest.raises(AgentRunCapacityError):
            async with acquire_agent_run_slot():
                pass
    finally:
        lease.release()
