from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.exceptions import (
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentNotFoundError,
)
from app.core.http_client import close_http_client, create_http_client
from app.core.kafka_client import close_kafka_producer, create_kafka_producer
from app.core.logging import setup_logging
from app.core.redis_client import close_redis_client, create_redis_client
from app.routers import health, payments, webhooks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging()
    app.state.redis = await create_redis_client()
    app.state.kafka_producer = await create_kafka_producer()
    app.state.http_client = await create_http_client()
    yield
    await close_redis_client(app.state.redis)
    await close_kafka_producer(app.state.kafka_producer)
    await close_http_client(app.state.http_client)


app = FastAPI(lifespan=lifespan)

app.include_router(payments.router, tags=["payments"])
app.include_router(webhooks.router, tags=["webhooks"])
app.include_router(health.router, tags=["health"])


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.exception_handler(IdempotencyConflictError)
async def idempotency_conflict_handler(
    request: Request, exc: IdempotencyConflictError
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(PaymentNotFoundError)
async def payment_not_found_handler(
    request: Request, exc: PaymentNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(InvalidStateTransitionError)
async def invalid_state_handler(
    request: Request, exc: InvalidStateTransitionError
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
