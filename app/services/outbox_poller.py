import json
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from aiokafka import AIOKafkaProducer
from app.models.outbox_event import OutboxEvent
from datetime import datetime, timezone

logger = structlog.get_logger()

async def poll_and_publish(
    db: AsyncSession,
    kafka_producer: AIOKafkaProducer,
    batch_size: int = 100
) -> int:
    result = await db.execute(
        select(OutboxEvent)
        .where(OutboxEvent.published_at == None)
        .limit(batch_size)
    )
    events = result.scalars().all()

    published_count = 0
    for event in events:
        try:
            await kafka_producer.send_and_wait(
                event.event_type,
                json.dumps(event.payload).encode("utf-8")
            )
            event.published_at = datetime.now(timezone.utc)
            published_count += 1
            logger.info("event_published", event_id=str(event.id))
        except Exception as e:
            logger.error("kafka_publish_failed", event_id=str(event.id), error=str(e))
            continue

    await db.commit()
    return published_count
