import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import outbox_lag_seconds
from app.models.outbox_event import OutboxEvent

logger = structlog.get_logger()


async def poll_and_publish(
    db: AsyncSession, kafka_producer: Any, batch_size: int = 100
) -> tuple[int, float]:
    result = await db.execute(
        select(OutboxEvent).where(OutboxEvent.published_at.is_(None)).limit(batch_size)
    )
    events = result.scalars().all()

    if not events:
        outbox_lag_seconds.set(0)
        return 0, 0.0

    oldest = min(e.created_at for e in events)
    lag = (datetime.now(UTC) - oldest).total_seconds()
    outbox_lag_seconds.set(lag)

    count = 0
    for event in events:
        try:
            await kafka_producer.send_and_wait(
                event.event_type, json.dumps(event.payload).encode("utf-8")
            )
            event.published_at = datetime.now(UTC)
            count += 1
        except Exception as e:
            logger.error("event_publish_failed", event_id=str(event.id), error=str(e))

    await db.commit()
    return count, lag
