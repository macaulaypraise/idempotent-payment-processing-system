"""Shared plumbing for idempotent write routes."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.core.exceptions import IdempotencyConflictError, IdempotencyKeyMismatchError
from app.core.metrics import idempotent_requests_total
from app.services.idempotency import (
    HandlerResult,
    IdempotentResult,
    ReplaySource,
    execute_idempotent,
    fingerprint,
)
from app.services.response_cache import ResponseCache

IDEMPOTENT_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "Idempotency-Key header missing or too long"},
    401: {"description": "Missing or invalid API key"},
    409: {"description": "Original request with this key still in progress"},
    422: {
        "description": "Invalid body, rejected request, or key reused with a "
        "different body"
    },
}


def outcome_of(result: IdempotentResult) -> str:
    if result.source is ReplaySource.CACHE:
        return "replayed_cache"
    if result.source is ReplaySource.DATABASE:
        return "replayed_db"
    return "rejected" if result.response.status_code >= 400 else "created"


async def idempotent_response(
    *,
    route: str,
    request: Request,
    db: AsyncSession,
    cache: ResponseCache,
    clock: Clock,
    settings: Settings,
    tenant_id: uuid.UUID,
    key: str,
    handler: Callable[[], Awaitable[HandlerResult]],
) -> tuple[JSONResponse, str]:
    """Run `handler` once per (tenant, key); return the response and outcome."""
    request_fingerprint = fingerprint(
        request.method, request.url.path, await request.json()
    )
    try:
        result = await execute_idempotent(
            db,
            cache,
            tenant_id=tenant_id,
            key=key,
            request_fingerprint=request_fingerprint,
            handler=handler,
            clock=clock,
            ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS,
            lock_timeout_ms=settings.IDEMPOTENCY_LOCK_TIMEOUT_MS,
        )
    except IdempotencyKeyMismatchError:
        idempotent_requests_total.labels(route=route, outcome="mismatch").inc()
        raise
    except IdempotencyConflictError:
        idempotent_requests_total.labels(route=route, outcome="conflict").inc()
        raise

    outcome = outcome_of(result)
    idempotent_requests_total.labels(route=route, outcome=outcome).inc()
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    response = JSONResponse(
        status_code=result.response.status_code,
        content=result.response.body,
        headers=headers,
    )
    return response, outcome
