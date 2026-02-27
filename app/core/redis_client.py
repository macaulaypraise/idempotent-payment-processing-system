import redis.asyncio as redis
from app.config import get_settings

settings = get_settings()

async def create_redis_client() -> redis.Redis:
    client = redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True
    )
    await client.ping()
    return client

async def close_redis_client(client: redis.Redis) -> None:
    await client.close()
