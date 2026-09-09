# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.

"""Unit tests for the dry-run-first A2A reputation utility."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.seed_a2a_reputation import _execute, _preflight, build_plan, main, plan_sha256


def _caller_ids() -> list[str]:
    return [f"org-{index}" for index in range(1, 6)]


def _plan() -> dict:
    return build_plan(
        _caller_ids(),
        "https://target.example.com",
        "Return a short capability check",
    )


def test_build_plan_requires_five_distinct_callers():
    with pytest.raises(ValueError, match="At least 5"):
        build_plan(_caller_ids()[:4], "https://target.example.com", "check")

    with pytest.raises(ValueError, match="distinct"):
        build_plan([*_caller_ids()[:4], "org-1"], "https://target.example.com", "check")


def test_build_plan_requires_https_target():
    with pytest.raises(ValueError, match="valid HTTPS"):
        build_plan(_caller_ids(), "http://target.example.com", "check")


def test_plan_digest_is_stable():
    plan = _plan()

    assert plan_sha256(plan) == plan_sha256(dict(plan))


def test_execute_requires_explicit_confirmation():
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                *sum((["--caller-org-id", org_id] for org_id in _caller_ids()), []),
                "--target-url",
                "https://target.example.com",
                "--task-description",
                "check",
                "--execute",
            ]
        )

    assert exc_info.value.code == 2


@pytest.mark.anyio
async def test_preflight_requires_registered_target_and_allowlist(monkeypatch):
    pool = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"org_id": "target-org"}),
    )
    settings = SimpleNamespace(
        marketplace_enabled=True,
        a2a_delegation_enabled=True,
        a2a_delegation_billing_enabled=True,
    )
    allowlist_mock = AsyncMock(return_value=(True, {"max_cost_usdc": 50_000}))
    monkeypatch.setattr("scripts.seed_a2a_reputation.get_settings", lambda: settings)
    monkeypatch.setattr("scripts.seed_a2a_reputation.async_validate_url", AsyncMock(return_value=None))
    monkeypatch.setattr("scripts.seed_a2a_reputation.check_delegation_allowed", allowlist_mock)

    await _preflight(_plan(), pool)

    assert allowlist_mock.await_count == 5
    assert all(call.args[1] == "https://target.example.com" for call in allowlist_mock.await_args_list)


@pytest.mark.anyio
async def test_execute_closes_billing_when_initialization_fails(monkeypatch):
    pool = SimpleNamespace(close=AsyncMock())
    settings = SimpleNamespace(pg_dsn="postgres://example")
    monkeypatch.setattr("scripts.seed_a2a_reputation.get_settings", lambda: settings)
    monkeypatch.setattr("scripts.seed_a2a_reputation.create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        "scripts.seed_a2a_reputation.init_billing",
        AsyncMock(side_effect=RuntimeError("facilitator unavailable")),
    )
    close_billing_mock = AsyncMock()
    monkeypatch.setattr("scripts.seed_a2a_reputation.close_billing", close_billing_mock)

    with pytest.raises(RuntimeError, match="facilitator unavailable"):
        await _execute(_plan(), None, "")

    close_billing_mock.assert_awaited_once()
    pool.close.assert_awaited_once()
