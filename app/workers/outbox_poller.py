import asyncio
import structlog
from app.core.database import AsyncSessionFactory
from app.core.kafka_client import create_kafka_producer, close_kafka_producer
from app.services.outbox_poller import poll_and_publish
from app.core.metrics import outbox_lag_seconds, dlq_depth
from datetime import datetime, timezone

logger = structlog.get_logger()


async def run_outbox_poller():
    logger.info("outbox_poller_starting")

    kafka_producer = await create_kafka_producer()

    try:
        while True:
            try:
                async with AsyncSessionFactory() as db:
                    count, lag = await poll_and_publish(db, kafka_producer)
                    outbox_lag_seconds.set(lag)

                if count > 0:
                    logger.info("outbox_events_published", count=count)
                    continue
                else:
                    await asyncio.sleep(2)

            except Exception as e:
                logger.error("outbox_poller_error", error=str(e))
                await asyncio.sleep(2)

    finally:
        await close_kafka_producer(kafka_producer)
        logger.info("outbox_poller_stopped")


if __name__ == "__main__":
    asyncio.run(run_outbox_poller())
