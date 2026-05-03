import asyncio

import pytest

from tools.definitions._rpc_semaphore import (
    acquire_chain_semaphore,
    acquire_rpc_semaphore,
    get_chain_cooldown_wait,
    get_chain_semaphore,
    get_rpc_semaphore,
    init_chain_semaphore,
    init_rpc_semaphore,
    set_chain_cooldown,
)


@pytest.mark.asyncio
async def test_rpc_semaphore_limit():
    """Test that the RPC semaphore correctly limits concurrency."""
    limit = 2
    init_rpc_semaphore(limit)

    sem = get_rpc_semaphore()
    assert sem._value == limit

    # Acquire all available slots
    async with acquire_rpc_semaphore():
        async with acquire_rpc_semaphore():
            assert sem._value == 0

            # Try to acquire one more with a timeout
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(acquire_rpc_semaphore().__aenter__(), timeout=0.1)


@pytest.mark.asyncio
async def test_rpc_semaphore_release():
    """Test that the RPC semaphore slots are released after use."""
    limit = 5
    init_rpc_semaphore(limit)
    sem = get_rpc_semaphore()

    for _ in range(10):
        async with acquire_rpc_semaphore():
            pass

    assert sem._value == limit


@pytest.mark.asyncio
async def test_rpc_semaphore_reinit():
    """Test that re-initializing the semaphore works (useful for tests)."""
    init_rpc_semaphore(10)
    assert get_rpc_semaphore()._value == 10

    init_rpc_semaphore(5)
    assert get_rpc_semaphore()._value == 5


@pytest.mark.asyncio
async def test_chain_semaphore_limit():
    init_chain_semaphore(1, 1)
    sem = get_chain_semaphore(1)
    assert sem._value == 1

    async with acquire_chain_semaphore(1):
        assert sem._value == 0
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(acquire_chain_semaphore(1).__aenter__(), timeout=0.1)


@pytest.mark.asyncio
async def test_chain_semaphore_none_or_unregistered_is_noop():
    async with acquire_chain_semaphore(None):
        pass
    async with acquire_chain_semaphore(999_999):
        pass


def test_chain_cooldown_helpers_set_and_read_remaining_wait():
    set_chain_cooldown(1, 0.5)
    wait = get_chain_cooldown_wait(1)
    assert 0.0 < wait <= 0.5


@pytest.mark.asyncio
async def test_acquire_chain_semaphore_honors_shared_cooldown(monkeypatch):
    init_chain_semaphore(8453, 1)
    set_chain_cooldown(8453, 0.25)

    slept_for: list[float] = []

    async def _fake_sleep(seconds: float):
        slept_for.append(seconds)

    monkeypatch.setattr("tools.definitions._rpc_semaphore.asyncio.sleep", _fake_sleep)

    async with acquire_chain_semaphore(8453):
        pass

    assert slept_for
    assert slept_for[0] > 0
