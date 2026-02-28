from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from contextlib import asynccontextmanager
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from app.core.redis_client import create_redis_client, close_redis_client
from app.core.kafka_client import create_kafka_producer, close_kafka_producer
from app.core.http_client import create_http_client, close_http_client
from app.core.logging import setup_logging
from app.core.exceptions import IdempotencyConflictError, PaymentNotFoundError, InvalidStateTransitionError
from app.routers import payments, webhooks, health

@asynccontextmanager
async def lifespan(app: FastAPI):
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
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.exception_handler(IdempotencyConflictError)
async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(PaymentNotFoundError)
async def payment_not_found_handler(request: Request, exc: PaymentNotFoundError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(InvalidStateTransitionError)
async def invalid_state_handler(request: Request, exc: InvalidStateTransitionError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
