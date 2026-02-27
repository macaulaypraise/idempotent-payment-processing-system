from fastapi import Depends, Request
import redis.asyncio as redis
from app.config import Settings, get_settings
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db

def get_settings_dep(settings: Settings = Depends(get_settings)) -> Settings:
    return settings

async def get_db_dp(db: AsyncSession = Depends (get_db)) -> AsyncSession:
    return db

async def get_redis(request: Request) -> redis.Redis:
    return request.app.state.redis
