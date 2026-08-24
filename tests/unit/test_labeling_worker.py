# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from labeling import worker


@pytest.mark.anyio
async def test_planning_failure_at_retry_limit_becomes_unavailable(monkeypatch):
    now = datetime.now(timezone.utc)
    row = {
        "id": "target-1",
        "lease_token": "lease-1",
        "attempts": 5,
        "item_key": "root",
        "item_payload": {"value": 1},
        "window_start": now,
        "window_end": now + timedelta(days=1),
        "definition_key": "example",
        "definition_version": 1,
        "prediction_schema": {},
        "target_schema": {},
        "outcome_schema": {},
        "parser_key": "parser",
        "parser_version": "1",
        "provider_key": "provider",
        "provider_version": "1",
        "scorer_key": "scorer",
        "scorer_version": "1",
        "config": {},
    }
    provider = MagicMock()
    provider.plan.side_effect = ValueError("poison target")
    complete = AsyncMock(return_value=True)
    retry = AsyncMock()
    monkeypatch.setattr(worker, "resolve_provider", lambda *_: provider)
    monkeypatch.setattr(worker, "complete_target", complete)
    monkeypatch.setattr(worker, "retry_target", retry)

    assert await worker._process_claimed_rows([row]) == 0

    retry.assert_not_awaited()
    complete.assert_awaited_once()
    result = complete.await_args.kwargs["result"]
    assert result.status == "unavailable"
    assert result.label == "unavailable"
