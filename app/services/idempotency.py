import json
from redis.asyncio import Redis

async def get_cached_response(redis: Redis,
    idempotency_key: str) -> dict | None:
    cache_key = f"idempotency:{idempotency_key}"
    result = await redis.get(cache_key)
    if result is None:
        return None
    return json.loads(result)

async def cache_response(
    redis: Redis,
    idempotency_key: str,
    response_body: dict,
    status_code: int,
    ttl: int = 86400
) -> None:
    cache_key = f"idempotency:{idempotency_key}"
    data = json.dumps({"body": response_body, "status_code": status_code})
    await redis.set(cache_key, data, ex=ttl)

async def acquire_lock(
    redis: Redis,
    idempotency_key: str,
    ttl: int = 30
) -> bool:
    lock_key = f"lock:{idempotency_key}"
    acquired = await redis.setnx(lock_key, "locked")
    if acquired:
        await redis.expire(lock_key, ttl)
    return bool(acquired)

async def release_lock(
    redis: Redis,
    idempotency_key: str
) -> None:
    lock_key = f"lock:{idempotency_key}"
    await redis.delete(lock_key)
