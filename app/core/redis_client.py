import redis.asyncio as redis
import structlog

from app.config import get_settings

logger = structlog.get_logger()


def build_redis_client(url: str, socket_timeout: float) -> redis.Redis:
    """A Redis client that fails fast: Redis is a cache, so a slow or dead
    Redis must cost milliseconds, not hang the request."""
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_timeout,
        retry_on_timeout=False,
        health_check_interval=0,
    )


async def create_redis_client() -> redis.Redis:
    settings = get_settings()
    client = build_redis_client(
        settings.REDIS_URL, settings.REDIS_SOCKET_TIMEOUT_SECONDS
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - startup must not depend on a cache
        logger.warning("redis_unavailable_at_startup", error=str(exc))
    return client


async def close_redis_client(client: redis.Redis) -> None:
    await client.aclose()
