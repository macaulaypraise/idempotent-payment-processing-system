import asyncio
import json
import structlog
from app.config import get_settings
from app.core.database import AsyncSessionFactory
from app.core.http_client import create_http_client, close_http_client
from app.core.kafka_client import (
    create_kafka_producer, close_kafka_producer,
    create_kafka_consumer, close_kafka_consumer
)
from app.services.state_machine import PaymentStatus, validate_transition
from sqlalchemy import select
from app.models.payment import Payment
from app.core.metrics import dlq_depth

logger = structlog.get_logger()
settings = get_settings()

MAX_RETRIES = 5


async def process_message(db, http_client, kafka_producer, payment_id: str, retry_count: int):
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if not payment:
        logger.error("payment_not_found", payment_id=payment_id)
        return

    validate_transition(payment.status, PaymentStatus.PROCESSING)
    payment.status = PaymentStatus.PROCESSING
    await db.commit()

    try:
        response = await http_client.post(
            "http://mock-payment-provider:8001/charge",
            json={"payment_id": payment_id, "amount": str(payment.amount), "currency": payment.currency}
        )
        response.raise_for_status()

        validate_transition(payment.status, PaymentStatus.SETTLED)
        payment.status = PaymentStatus.SETTLED
        await db.commit()
        logger.info("payment_settled", payment_id=payment_id)

        await kafka_producer.send_and_wait(
            "payment.completed",
            json.dumps({"payment_id": payment_id, "status": "settled"}).encode("utf-8")
        )

    except Exception as e:
        logger.error("provider_call_failed", payment_id=payment_id, error=str(e), retry_count=retry_count)

        if retry_count >= MAX_RETRIES:
            payment.status = PaymentStatus.FAILED
            await db.commit()
            await kafka_producer.send_and_wait(
                "payment.dlq",
                json.dumps({"payment_id": payment_id, "error": str(e)}).encode("utf-8")
            )
            dlq_depth.inc()
            logger.error("payment_moved_to_dlq", payment_id=payment_id)
        else:
            backoff = 2 ** retry_count
            await asyncio.sleep(backoff)
            await kafka_producer.send_and_wait(
                "payment.initiated",
                json.dumps({"payment_id": payment_id, "retry_count": retry_count + 1}).encode("utf-8")
            )
            logger.info("payment_requeued", payment_id=payment_id, next_retry=retry_count + 1)


async def run_settlement_worker():
    logger.info("settlement_worker_starting")

    consumer = await create_kafka_consumer(
        topic="payment.initiated",
        group_id="settlement-worker"
    )
    kafka_producer = await create_kafka_producer()
    http_client = await create_http_client()

    try:
        async for message in consumer:
            try:
                payload = json.loads(message.value)
                payment_id = payload["payment_id"]
                retry_count = payload.get("retry_count", 0)

                logger.info("processing_payment", payment_id=payment_id, retry_count=retry_count)

                async with AsyncSessionFactory() as db:
                    await process_message(db, http_client, kafka_producer, payment_id, retry_count)

            except Exception as e:
                logger.error("settlement_worker_error", error=str(e))
            finally:
                await consumer.commit()

    finally:
        await close_kafka_consumer(consumer)
        await close_kafka_producer(kafka_producer)
        await close_http_client(http_client)
        logger.info("settlement_worker_stopped")


if __name__ == "__main__":
    asyncio.run(run_settlement_worker())
