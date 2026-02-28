import json
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from aiokafka import AIOKafkaProducer
from app.models.outbox_event import OutboxEvent
from app.core.metrics import outbox_lag_seconds
from datetime import datetime, timezone

logger = structlog.get_logger()

async def poll_and_publish(db, kafka_producer, batch_size=100) -> tuple[int, float]:
    result = await db.execute(
        select(OutboxEvent)
        .where(OutboxEvent.published_at == None)
        .limit(batch_size)
    )
    events = result.scalars().all()

    if not events:
        outbox_lag_seconds.set(0)
        return 0, 0.0

    oldest = min(e.created_at for e in events)
    lag = (datetime.now(timezone.utc) - oldest).total_seconds()
    outbox_lag_seconds.set(lag)

    count = 0
    for event in events:
        try:
            await kafka_producer.send_and_wait(
                event.event_type,
                json.dumps(event.payload).encode("utf-8")
            )
            event.published_at = datetime.now(timezone.utc)
            count += 1
        except Exception as e:
            logger.error("event_publish_failed", event_id=str(event.id), error=str(e))

    await db.commit()
    return count, lag
