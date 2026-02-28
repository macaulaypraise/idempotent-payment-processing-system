import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from redis.asyncio import Redis
from app.core.database import Base

TEST_DATABASE_URL = "postgresql+asyncpg://IPPS:IPPS_password@localhost:5432/IPPSDB"
TEST_REDIS_URL = "redis://localhost:6379/1"


@pytest_asyncio.fixture(scope="function")
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "TRUNCATE TABLE payments, outbox_events, processed_events RESTART IDENTITY CASCADE"
        ))

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as session:
        yield session

    await engine.dispose()

@pytest.fixture
def mock_kafka_producer(mocker):
    """Mocks Kafka producer — integration tests shouldn't need real Kafka."""
    producer = mocker.AsyncMock()
    producer.send_and_wait = mocker.AsyncMock(return_value=None)
    return producer

@pytest_asyncio.fixture(scope="function")
async def redis_client():
    client = Redis.from_url(TEST_REDIS_URL)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()
