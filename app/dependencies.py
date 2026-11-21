import uuid
from typing import cast

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import PspClient, build_psp_client
from app.config import Settings, get_settings
from app.core.database import get_db
from app.core.exceptions import (
    ForbiddenError,
    IdempotencyKeyMissingError,
    UnauthorizedError,
)
from app.core.security import hash_api_key, parse_bearer_token
from app.models.tenant import DEFAULT_TENANT_ID, ApiKey
from app.services.response_cache import ResponseCache


def get_settings_dep(settings: Settings = Depends(get_settings)) -> Settings:
    return settings


async def get_db_dep(db: AsyncSession = Depends(get_db)) -> AsyncSession:
    return db


async def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


async def get_response_cache(request: Request) -> ResponseCache:
    return cast(ResponseCache, request.app.state.response_cache)


async def get_current_tenant(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_dep),
) -> uuid.UUID:
    """Resolve `Authorization: Bearer <api key>` to a tenant id, or 401."""
    token = parse_bearer_token(authorization)
    if token is None:
        raise UnauthorizedError()
    tenant_id = (
        await db.execute(
            select(ApiKey.tenant_id).where(
                ApiKey.key_hash == hash_api_key(token), ApiKey.revoked_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    # End the read-only transaction so the request's write transaction starts
    # fresh (SET LOCAL lock_timeout must be inside that transaction).
    await db.rollback()
    if tenant_id is None:
        raise UnauthorizedError()
    return cast(uuid.UUID, tenant_id)


async def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings_dep),
) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise IdempotencyKeyMissingError()
    key = idempotency_key.strip()
    if len(key) > settings.IDEMPOTENCY_KEY_MAX_LENGTH:
        raise IdempotencyKeyMissingError(
            f"Idempotency-Key must be at most {settings.IDEMPOTENCY_KEY_MAX_LENGTH} "
            "characters"
        )
    return key


async def require_platform_tenant(
    tenant_id: uuid.UUID = Depends(get_current_tenant),
) -> uuid.UUID:
    """Platform operations (reconciliation) settle every tenant's money, so
    only API keys of the platform tenant may run or read them."""
    if tenant_id != DEFAULT_TENANT_ID:
        raise ForbiddenError("reconciliation needs a platform API key")
    return tenant_id


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """For work that spans many transactions (one per settlement line)."""
    from app.core.database import AsyncSessionFactory

    return AsyncSessionFactory


async def get_psp_client(
    request: Request, settings: Settings = Depends(get_settings_dep)
) -> PspClient:
    client = getattr(request.app.state, "psp_client", None)
    if client is None:
        client = build_psp_client(settings.PSP_BASE_URL, settings.PSP_TIMEOUT_SECONDS)
        request.app.state.psp_client = client
    return cast(PspClient, client)
