"""Unit tests for app/services/response_cache.py: Redis failures are misses."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.response_cache import ResponseCache, StoredResponse

TENANT = uuid.uuid4()
RESPONSE = StoredResponse(202, {"payment_id": "p1"}, "fp")


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _cache(redis: AsyncMock | None, clock: Clock | None = None) -> ResponseCache:
    return ResponseCache(
        redis, ttl_seconds=60, cooldown_seconds=5, monotonic=clock or Clock()
    )


def test_cache_key_is_tenant_scoped() -> None:
    a = ResponseCache.cache_key(TENANT, "k")
    b = ResponseCache.cache_key(uuid.uuid4(), "k")
    assert a != b
    assert str(TENANT) in a and a.endswith(":k")


@pytest.mark.asyncio
async def test_get_hit_round_trips_put() -> None:
    store: dict[str, str] = {}
    redis = AsyncMock()
    redis.set.side_effect = lambda k, v, ex: store.__setitem__(k, v)
    redis.get.side_effect = lambda k: store.get(k)
    cache = _cache(redis)

    await cache.put(TENANT, "k", RESPONSE)
    assert redis.set.call_args.kwargs["ex"] == 60
    assert await cache.get(TENANT, "k") == RESPONSE


@pytest.mark.asyncio
async def test_get_miss_returns_none() -> None:
    redis = AsyncMock()
    redis.get.return_value = None
    assert await _cache(redis).get(TENANT, "k") is None


@pytest.mark.asyncio
async def test_no_redis_configured_is_always_a_miss() -> None:
    cache = _cache(None)
    assert await cache.get(TENANT, "k") is None
    await cache.put(TENANT, "k", RESPONSE)  # no error


@pytest.mark.asyncio
async def test_redis_error_is_a_miss_and_opens_breaker() -> None:
    clock = Clock()
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("refused")
    cache = _cache(redis, clock)

    assert await cache.get(TENANT, "k") is None
    assert cache.breaker_open
    # While open, Redis is not called at all.
    assert await cache.get(TENANT, "k") is None
    await cache.put(TENANT, "k", RESPONSE)
    assert redis.get.await_count == 1
    redis.set.assert_not_awaited()

    # After the cooldown it tries again.
    clock.t += 5.1
    redis.get.side_effect = None
    redis.get.return_value = None
    assert not cache.breaker_open
    assert await cache.get(TENANT, "k") is None
    assert redis.get.await_count == 2


@pytest.mark.asyncio
async def test_put_error_opens_breaker_without_raising() -> None:
    redis = AsyncMock()
    redis.set.side_effect = TimeoutError("slow")
    cache = _cache(redis)
    await cache.put(TENANT, "k", RESPONSE)
    assert cache.breaker_open


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", json.dumps({"body": {}}), "[]"])
async def test_corrupt_entry_is_a_miss(raw: str) -> None:
    redis = AsyncMock()
    redis.get.return_value = raw
    cache = _cache(redis)
    assert await cache.get(TENANT, "k") is None
    assert not cache.breaker_open
