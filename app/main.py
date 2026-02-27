from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.core.redis_client import create_redis_client, close_redis_client
from app.core.kafka_client import create_kafka_producer, create_kafka_consumer, close_kafka_producer, close_kafka_consumer
from app.core.http_client import create_http_client, close_http_client
from app.core.logging import setup_logging

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.redis = await create_redis_client()
    app.state.kafka_producer = await create_kafka_producer()
    app.state.kafka_consumer = await create_kafka_consumer(
        topic="payment.initiated",
        group_id="settlement-worker"
    )
    app.state.http_client = await create_http_client()
    yield
    await close_redis_client(app.state.redis)
    await close_kafka_producer(app.state.kafka_producer)
    await close_kafka_consumer(app.state.kafka_consumer)
    await close_http_client(app.state.http_client)

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health():
    return {"status": "ok"}
