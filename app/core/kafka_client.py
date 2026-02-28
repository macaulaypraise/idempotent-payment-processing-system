from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from app.config import get_settings

settings = get_settings()

async def create_kafka_producer() -> AIOKafkaProducer:
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
    )
    await producer.start()
    return producer

async def close_kafka_producer(producer: AIOKafkaProducer) -> None:
    await producer.stop()

async def create_kafka_consumer(topic: str, group_id: str) -> AIOKafkaConsumer:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id=group_id,
        value_deserializer=lambda v: v.decode("utf-8")
    )
    await consumer.start()
    return consumer

async def close_kafka_consumer(consumer: AIOKafkaConsumer) -> None:
    await consumer.stop()
