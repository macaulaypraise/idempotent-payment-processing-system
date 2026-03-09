from typing import Any

from aiokafka.admin import AIOKafkaAdminClient
from fastapi import APIRouter, Request
from sqlalchemy import text

from app.config import get_settings
from app.core.database import engine

router = APIRouter()
settings = get_settings()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    try:
        await request.app.state.redis.ping()
        redis_status = "ok"
    except Exception:
        redis_status = "error"

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"

    try:
        admin = AIOKafkaAdminClient(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)
        await admin.start()
        await admin.close()
        kafka_status = "ok"
    except Exception:
        kafka_status = "error"

    overall = (
        "ok"
        if all(s == "ok" for s in [redis_status, db_status, kafka_status])
        else "degraded"
    )

    return {
        "status": overall,
        "redis": redis_status,
        "db": db_status,
        "kafka": kafka_status,
    }
