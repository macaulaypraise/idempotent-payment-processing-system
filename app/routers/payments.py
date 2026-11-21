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
from app.routers.idempotent import IDEMPOTENT_ERRORS, idempotent_response
from app.schemas.payment import PaymentRequest, PaymentResponse
from app.services.payment_service import create_payment, get_payment, payment_body
from app.services.response_cache import ResponseCache

router = APIRouter(prefix="/v1")

# v1 metric labels, kept so existing dashboards keep working.
_V1_LABEL = {
    "created": "success",
    "replayed_db": "idempotent_hit",
    "replayed_cache": "cache_hit",
    "rejected": "rejected",
}


@router.post(
    "/payments",
    response_model=PaymentResponse,
    status_code=202,
    responses=IDEMPOTENT_ERRORS,
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
    try:
        response, outcome = await idempotent_response(
            route="payments",
            request=request,
            db=db,
            cache=cache,
            clock=clock,
            settings=settings,
            tenant_id=tenant_id,
            key=idempotency_key,
            handler=lambda: create_payment(
                db,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                amount=payload.amount,
                currency=payload.currency,
                wallet_id=payload.wallet_id,
            ),
        )
    except IdempotencyKeyMismatchError:
        payments_created_total.labels(status="mismatch").inc()
        raise
    except IdempotencyConflictError:
        payments_created_total.labels(status="conflict").inc()
        raise
    payments_created_total.labels(status=_V1_LABEL[outcome]).inc()
    return response


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
