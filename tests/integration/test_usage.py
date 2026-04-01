"""Integration tests for usage.py — record and aggregate usage events."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import usage as usage_module
import users as user_module
from usage import UsageEvent, get_usage_by_org, get_usage_by_user, record_usage_event
from users import create_org, create_user


@pytest.fixture(autouse=True)
def bind_pools(db_pool):
    user_module._pool = db_pool
    usage_module._pool = db_pool
    yield
    user_module._pool = None
    usage_module._pool = None


@pytest.fixture
async def test_user(db_pool):
    org = await create_org("Usage Org")
    return await create_user("usage_test@example.com", "pass", org.id)


def _make_event(user_id: str, org_id: str, **overrides) -> UsageEvent:
    defaults = dict(
        user_id=user_id,
        org_id=org_id,
        thread_id="thread-1",
        run_id="run-1",
        tokens_in=100,
        tokens_out=50,
        tool_calls=2,
        tool_names=["calculate", "get_datetime"],
        duration_ms=500,
    )
    defaults.update(overrides)
    return UsageEvent(**defaults)


@pytest.mark.anyio
async def test_record_usage_event(test_user):
    event = _make_event(test_user.id, test_user.org_id)
    # Should not raise.
    await record_usage_event(event)


@pytest.mark.anyio
async def test_get_usage_by_user_aggregation(test_user):
    e1 = _make_event(test_user.id, test_user.org_id, run_id="run-a", tokens_in=100)
    e2 = _make_event(test_user.id, test_user.org_id, run_id="run-b", tokens_in=200)
    await record_usage_event(e1)
    await record_usage_event(e2)

    summary = await get_usage_by_user(test_user.id)
    assert summary.total_runs == 2
    assert summary.total_tokens_in == 300


@pytest.mark.anyio
async def test_get_usage_by_org_aggregation(test_user):
    e1 = _make_event(test_user.id, test_user.org_id, run_id="run-c", tokens_out=80)
    e2 = _make_event(test_user.id, test_user.org_id, run_id="run-d", tokens_out=120)
    await record_usage_event(e1)
    await record_usage_event(e2)

    summary = await get_usage_by_org(test_user.org_id)
    assert summary.total_tokens_out >= 200


@pytest.mark.anyio
async def test_no_events_returns_zero_summary(test_user):
    summary = await get_usage_by_user("nonexistent-user-id")
    assert summary.total_runs == 0
    assert summary.total_tokens_in == 0


@pytest.mark.anyio
async def test_date_range_filtering(test_user):
    event = _make_event(test_user.id, test_user.org_id, run_id="run-e")
    await record_usage_event(event)

    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    summary = await get_usage_by_user(test_user.id, end=future)
    assert summary.total_runs >= 1

    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    empty_summary = await get_usage_by_user(test_user.id, start=future, end=future)
    assert empty_summary.total_runs == 0


@pytest.mark.anyio
async def test_fire_and_forget_logs_not_raises(test_user, monkeypatch):
    """record_usage_event must not propagate exceptions."""
    import usage

    async def _boom(*args, **kwargs):
        raise RuntimeError("DB exploded")

    monkeypatch.setattr(usage, "_get_pool", lambda: (_ for _ in ()).throw(RuntimeError("no pool")))
    # Should not raise:
    await record_usage_event(_make_event(test_user.id, test_user.org_id, run_id="boom"))
