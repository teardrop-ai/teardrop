import asyncio
import pytest
from unittest.mock import MagicMock
from tools.definitions._rpc_semaphore import init_rpc_semaphore, acquire_rpc_semaphore, get_rpc_semaphore

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
