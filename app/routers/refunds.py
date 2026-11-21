import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock, get_clock
from app.dependencies import (
    get_current_tenant,
    get_db_dep,
    get_response_cache,
    get_settings_dep,
    require_idempotency_key,
)
from app.routers.idempotent import IDEMPOTENT_ERRORS, idempotent_response
from app.schemas.payment import RefundRequest, RefundResponse
from app.services.refunds import get_refund, refund_body, request_refund
from app.services.response_cache import ResponseCache

router = APIRouter(prefix="/v1")


@router.post(
    "/refunds",
    response_model=RefundResponse,
    status_code=201,
    responses={**IDEMPOTENT_ERRORS, 404: {"description": "Payment not found"}},
)
async def create_refund_route(
    request: Request,
    payload: RefundRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db_dep),
    cache: ResponseCache = Depends(get_response_cache),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Request a full refund. The amount is held in refunds_payable at once;
    the provider's answer later confirms it or returns the hold."""
    response, _ = await idempotent_response(
        route="refunds",
        request=request,
        db=db,
        cache=cache,
        clock=clock,
        settings=settings,
        tenant_id=tenant_id,
        key=idempotency_key,
        handler=lambda: request_refund(
            db, tenant_id=tenant_id, payment_id=payload.payment_id
        ),
    )
    return response


@router.get("/refunds/{refund_id}", response_model=RefundResponse)
async def get_refund_route(
    refund_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db_dep),
) -> JSONResponse:
    refund = await get_refund(db, tenant_id=tenant_id, refund_id=refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail=f"Refund {refund_id} not found")
    # Same serialisation as the POST body, so GET and replay agree byte for byte
    return JSONResponse(refund_body(refund))
