import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv(".env.test")
TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "")
TEST_REDIS_URL: str = os.environ.get("TEST_REDIS_URL", "")
# Nothing listens on port 1: connections are refused immediately, which is
# how "Redis is down" looks to the application.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
# The app runs as a restricted role, as in production. A separate name from
# the dev stack's role keeps the two from overwriting each other's password.
TEST_APP_ROLE = "ipps_app_test"
TEST_APP_PASSWORD = "ipps_app_test"
_CLEAN_SQL = (
    # Ledger tables are append-only even for the owner; replica mode skips the
    # immutability triggers so tests can start clean (owner is a superuser in
    # docker-compose and CI).
    "SET session_replication_role = replica",
    "TRUNCATE TABLE entries, transfers, wallets, accounts, idempotency_keys, "
    "api_keys, outbox_events, processed_events, payments RESTART IDENTITY CASCADE",
    f"DELETE FROM tenants WHERE id <> '{DEFAULT_TENANT_ID}'",
    "SET session_replication_role = DEFAULT",
)


def app_role_url(owner_url: str) -> str:
    url = make_url(owner_url).set(username=TEST_APP_ROLE, password=TEST_APP_PASSWORD)
    return url.render_as_string(hide_password=False)


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


async def _configure_role(owner_url: str) -> None:
    from app.cli import configure_app_role

    engine = create_async_engine(owner_url)
    async with engine.begin() as conn:
        await configure_app_role(conn, TEST_APP_ROLE, TEST_APP_PASSWORD)
    await engine.dispose()


@pytest.fixture(scope="session")
def owner_database_url() -> str:
    """Build the test schema with Alembic, as the owner, exactly as production.

    Constraints and triggers written as raw SQL in migrations exist only this
    way; Base.metadata.create_all would silently skip them.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in db_name:
        # This fixture drops the whole schema; refuse anything but a test DB.
        raise RuntimeError(f"refusing to reset non-test database {db_name!r}")
    asyncio.run(_reset_schema(TEST_DATABASE_URL))
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["app_role"] = TEST_APP_ROLE
    command.upgrade(cfg, "head")
    asyncio.run(_configure_role(TEST_DATABASE_URL))
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database(owner_database_url: str) -> str:
    """URL for the restricted application role."""
    return app_role_url(owner_database_url)


@pytest_asyncio.fixture
async def owner_session_factory(
    owner_database_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(owner_database_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    migrated_database: str, owner_database_url: str
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    owner = create_async_engine(owner_database_url)
    async with owner.begin() as conn:
        for statement in _CLEAN_SQL:
            await conn.execute(text(statement))
    await owner.dispose()

    engine = create_async_engine(
        migrated_database, echo=False, pool_size=10, max_overflow=10
    )
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session


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


@dataclass(frozen=True)
class TenantCredentials:
    tenant_id: uuid.UUID
    api_key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@pytest.fixture
def make_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[str], Any]:
    async def _make(name: str = "acme") -> TenantCredentials:
        from app.cli import create_tenant

        async with session_factory() as db:
            tenant_id, api_key = await create_tenant(db, name)
        return TenantCredentials(tenant_id, api_key)

    return _make


@pytest.fixture
def api_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Any]:
    """Factory for an in-process HTTP client wired to the real test database.

    Every request gets its own session (like production), so concurrency
    tests exercise real transaction interleavings. `redis_url` chooses a
    working or a dead Redis; settings overrides adjust e.g. lock_timeout.
    """

    @asynccontextmanager
    async def _client(
        redis_url: str = TEST_REDIS_URL, **settings_overrides: Any
    ) -> AsyncIterator[AsyncClient]:
        from app.config import get_settings
        from app.core.redis_client import build_redis_client
        from app.dependencies import get_db_dep, get_response_cache, get_settings_dep
        from app.main import app
        from app.services.response_cache import ResponseCache

        settings = get_settings().model_copy(update=settings_overrides)
        redis = build_redis_client(redis_url, settings.REDIS_SOCKET_TIMEOUT_SECONDS)
        cache = ResponseCache(
            redis,
            ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS,
            cooldown_seconds=settings.REDIS_BREAKER_COOLDOWN_SECONDS,
        )

        async def _db() -> AsyncGenerator[AsyncSession, None]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db_dep] = _db
        app.dependency_overrides[get_response_cache] = lambda: cache
        app.dependency_overrides[get_settings_dep] = lambda: settings
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client
        finally:
            app.dependency_overrides.clear()
            await redis.aclose()

    return _client
