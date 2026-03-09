from typing import cast

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.database import get_db


def get_settings_dep(settings: Settings = Depends(get_settings)) -> Settings:
    return settings


async def get_db_dep(db: AsyncSession = Depends(get_db)) -> AsyncSession:
    return db


async def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)
