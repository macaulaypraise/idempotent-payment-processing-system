from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.core.logging import setup_logging
from app.core.redis_client import close_redis_client, create_redis_client
from app.routers import (
    health,
    payments,
    reconciliations,
    refunds,
    wallets,
    webhooks,
)
from app.services.response_cache import ResponseCache


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # The request path needs only Postgres. Redis is an optional cache, and
    # Kafka is used by the outbox poller, not the API, so neither can stop
    # the API from starting.
    setup_logging()
    settings = get_settings()
    app.state.redis = await create_redis_client()
    app.state.response_cache = ResponseCache(
        app.state.redis,
        ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS,
        cooldown_seconds=settings.REDIS_BREAKER_COOLDOWN_SECONDS,
    )
    yield
    await close_redis_client(app.state.redis)


app = FastAPI(title="IPPS / Ledgerline", version="2.0.0-dev", lifespan=lifespan)

app.include_router(payments.router, tags=["payments"])
app.include_router(wallets.router, tags=["wallets"])
app.include_router(refunds.router, tags=["refunds"])
app.include_router(webhooks.router, tags=["webhooks"])
app.include_router(reconciliations.router, tags=["reconciliation"])
app.include_router(health.router, tags=["health"])


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
