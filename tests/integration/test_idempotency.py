import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from app.services.idempotency import (
    get_cached_response,
    cache_response,
    acquire_lock,
    release_lock
)

@pytest.mark.asyncio
async def test_idempotent_retry_returns_same_response():
    redis = AsyncMock()
    redis.get.return_value = '{"body": {"payment_id": "123"}, "status_code": 202}'

    result = await get_cached_response(redis, "test-key")
    assert result["body"]["payment_id"] == "123"

@pytest.mark.asyncio
async def test_acquire_lock_returns_true_when_available():
    redis = AsyncMock()
    redis.setnx.return_value = True

    result = await acquire_lock(redis, "test-key")
    assert result is True

@pytest.mark.asyncio
async def test_acquire_lock_returns_false_when_taken():
    redis = AsyncMock()
    redis.setnx.return_value = False

    result = await acquire_lock(redis, "test-key")
    assert result is False
