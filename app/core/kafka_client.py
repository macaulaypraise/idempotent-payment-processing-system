import ssl
from collections.abc import Sequence
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.config import get_settings

settings = get_settings()


def _kafka_ssl_kwargs() -> dict[str, Any]:
    """Returns SASL/SSL kwargs only when configured — empty dict for local dev."""
    if settings.KAFKA_SECURITY_PROTOCOL == "PLAINTEXT":
        return {}

    context = ssl.create_default_context()

    return {
        "security_protocol": settings.KAFKA_SECURITY_PROTOCOL,
        "sasl_mechanism": settings.KAFKA_SASL_MECHANISM,
        "sasl_plain_username": settings.KAFKA_SASL_USERNAME,
        "sasl_plain_password": settings.KAFKA_SASL_PASSWORD,
        "ssl_context": context,
    }


async def create_kafka_producer() -> AIOKafkaProducer:
    # Idempotent with acks=all: the broker drops the duplicate a producer
    # retry would otherwise write, and an acknowledged send survives the loss
    # of a broker. Ordering per partition holds across retries.
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        enable_idempotence=True,
        acks="all",
        **_kafka_ssl_kwargs(),
    )
    await producer.start()
    return producer


async def close_kafka_producer(producer: AIOKafkaProducer) -> None:
    await producer.stop()


async def create_kafka_consumer(
    topics: str | Sequence[str], group_id: str, *, auto_commit: bool = True
) -> AIOKafkaConsumer:
    names = [topics] if isinstance(topics, str) else list(topics)
    consumer = AIOKafkaConsumer(
        *names,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id=group_id,
        enable_auto_commit=auto_commit,
        value_deserializer=lambda v: v.decode("utf-8"),
        **_kafka_ssl_kwargs(),
    )
    await consumer.start()
    return consumer


async def close_kafka_consumer(consumer: AIOKafkaConsumer) -> None:
    await consumer.stop()
