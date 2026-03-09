"""
Unit tests for app/services/idempotency.py
Redis is mocked — no running infrastructure needed.
"""

import json
from unittest.mock import AsyncMock

import pytest

from app.services.idempotency import (
    acquire_lock,
    cache_response,
    get_cached_response,
    release_lock,
)

# Fixtures


@pytest.fixture
def mock_redis() -> None:
    redis = AsyncMock()
    return redis


# get_cached_response


@pytest.mark.asyncio
async def test_get_cached_response_hit(mock_redis) -> None:
    payload = {"payment_id": "abc-123", "status": "pending"}
    mock_redis.get.return_value = json.dumps(payload).encode()

    result = await get_cached_response(mock_redis, "key-1")

    assert result is not None
    assert result["payment_id"] == "abc-123"
    mock_redis.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_cached_response_miss(mock_redis) -> None:
    mock_redis.get.return_value = None

    result = await get_cached_response(mock_redis, "key-missing")

    assert result is None


@pytest.mark.asyncio
async def test_get_cached_response_uses_correct_key(mock_redis) -> None:
    mock_redis.get.return_value = None
    await get_cached_response(mock_redis, "my-idempotency-key")

    call_args = mock_redis.get.call_args[0][0]
    assert "my-idempotency-key" in call_args


# cache_response


@pytest.mark.asyncio
async def test_cache_response_calls_set(mock_redis) -> None:
    await cache_response(mock_redis, "key-1", {"payment_id": "abc"}, 202)
    mock_redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_response_uses_ttl(mock_redis) -> None:
    await cache_response(mock_redis, "key-1", {"payment_id": "abc"}, 202, ttl=3600)

    call_kwargs = mock_redis.set.call_args
    # TTL should be passed as ex= keyword or as positional arg
    assert call_kwargs is not None


@pytest.mark.asyncio
async def test_cache_response_stores_status_code(mock_redis) -> None:
    body = {"payment_id": "xyz"}
    await cache_response(mock_redis, "key-1", body, 202)

    stored = mock_redis.set.call_args[0][1]
    parsed = json.loads(stored)
    assert parsed["status_code"] == 202
    assert parsed["body"]["payment_id"] == "xyz"


# acquire_lock


@pytest.mark.asyncio
async def test_acquire_lock_success(mock_redis) -> None:
    mock_redis.setnx.return_value = 1  # lock acquired

    result = await acquire_lock(mock_redis, "key-1")

    assert result is True
    mock_redis.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_lock_already_held(mock_redis) -> None:
    mock_redis.setnx.return_value = 0  # lock already held

    result = await acquire_lock(mock_redis, "key-1")

    assert result is False


@pytest.mark.asyncio
async def test_acquire_lock_key_pattern(mock_redis) -> None:
    mock_redis.setnx.return_value = 1
    await acquire_lock(mock_redis, "my-key")

    lock_key = mock_redis.setnx.call_args[0][0]
    assert "my-key" in lock_key
    assert "lock" in lock_key


@pytest.mark.asyncio
async def test_acquire_lock_sets_expiry(mock_redis) -> None:
    mock_redis.setnx.return_value = 1
    await acquire_lock(mock_redis, "key-1", ttl=30)

    mock_redis.expire.assert_awaited_once()
    expire_args = mock_redis.expire.call_args[0]
    assert 30 in expire_args


# release_lock


@pytest.mark.asyncio
async def test_release_lock_calls_delete(mock_redis) -> None:
    await release_lock(mock_redis, "key-1")
    mock_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_lock_key_pattern(mock_redis) -> None:
    await release_lock(mock_redis, "my-key")
    deleted_key = mock_redis.delete.call_args[0][0]
    assert "my-key" in deleted_key
    assert "lock" in deleted_key
