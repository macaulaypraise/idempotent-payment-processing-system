import pytest
from unittest.mock import AsyncMock
from app.services.idempotency import (
    get_cached_response,
    cache_response,
    acquire_lock,
    release_lock
)

@pytest.mark.asyncio
async def test_cache_miss_returns_none():
    redis = AsyncMock()
    redis.get.return_value = None

    result = await get_cached_response(redis, "missing-key")
    assert result is None

@pytest.mark.asyncio
async def test_cache_response_stores_correctly():
    redis = AsyncMock()
    await cache_response(redis, "test-key", {"payment_id": "123"}, 202)
    redis.set.assert_called_once()

@pytest.mark.asyncio
async def test_release_lock_deletes_key():
    redis = AsyncMock()
    await release_lock(redis, "test-key")
    redis.delete.assert_called_once_with("lock:test-key")
