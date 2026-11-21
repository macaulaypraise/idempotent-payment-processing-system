import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock, get_clock
from app.core.exceptions import (
    IdempotencyConflictError,
    IdempotencyKeyMismatchError,
    PaymentNotFoundError,
)
from app.core.metrics import payments_created_total
from app.dependencies import (
    get_current_tenant,
    get_db_dep,
    get_response_cache,
    get_settings_dep,
    require_idempotency_key,
)
from app.schemas.payment import PaymentRequest, PaymentResponse
from app.services.idempotency import ReplaySource, execute_idempotent, fingerprint
from app.services.payment_service import create_payment, get_payment, payment_body
from app.services.response_cache import ResponseCache

router = APIRouter(prefix="/v1")

_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "Idempotency-Key header missing or too long"},
    401: {"description": "Missing or invalid API key"},
    409: {"description": "Original request with this key still in progress"},
    422: {"description": "Invalid body, or key reused with a different body"},
}


@router.post(
    "/payments", response_model=PaymentResponse, status_code=202, responses=_ERRORS
)
async def create_payment_route(
    request: Request,
    payload: PaymentRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db_dep),
    cache: ResponseCache = Depends(get_response_cache),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    request_fingerprint = fingerprint(
        request.method, request.url.path, await request.json()
    )
    try:
        result = await execute_idempotent(
            db,
            cache,
            tenant_id=tenant_id,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            handler=lambda: create_payment(
                db,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                amount=payload.amount,
                currency=payload.currency,
            ),
            clock=clock,
            ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS,
            lock_timeout_ms=settings.IDEMPOTENCY_LOCK_TIMEOUT_MS,
        )
    except IdempotencyKeyMismatchError:
        payments_created_total.labels(status="mismatch").inc()
        raise
    except IdempotencyConflictError:
        payments_created_total.labels(status="conflict").inc()
        raise

    if result.source is ReplaySource.CACHE:
        payments_created_total.labels(status="cache_hit").inc()
    elif result.source is ReplaySource.DATABASE:
        payments_created_total.labels(status="idempotent_hit").inc()
    elif result.response.status_code >= 400:
        payments_created_total.labels(status="rejected").inc()
    else:
        payments_created_total.labels(status="success").inc()

    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(
        status_code=result.response.status_code,
        content=result.response.body,
        headers=headers,
    )


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
async def get_payment_route(
    payment_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db_dep),
) -> dict[str, Any]:
    payment = await get_payment(db, tenant_id=tenant_id, payment_id=payment_id)
    if payment is None:
        # Same answer for "does not exist" and "belongs to another tenant".
        raise PaymentNotFoundError(str(payment_id))
    return {
        **payment_body(payment),
        "created_at": payment.created_at,
        "updated_at": payment.updated_at,
    }
