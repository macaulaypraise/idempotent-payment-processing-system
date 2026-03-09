import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base

load_dotenv(".env.test")
TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL: str = os.environ.get("TEST_REDIS_URL")


@pytest_asyncio.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "TRUNCATE TABLE payments, outbox_events, processed_events "
                "RESTART IDENTITY CASCADE"
            )
        )

    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def mock_kafka_producer(mocker: Any) -> Any:
    """Mocks Kafka producer — integration tests shouldn't need real Kafka."""
    producer = mocker.AsyncMock()
    producer.send_and_wait = mocker.AsyncMock(return_value=None)
    return producer


@pytest_asyncio.fixture(scope="function")
async def redis_client() -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(TEST_REDIS_URL)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()
