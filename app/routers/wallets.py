import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.models.wallet import Wallet
from app.routers.idempotent import IDEMPOTENT_ERRORS, idempotent_response
from app.schemas.wallet import (
    BalanceResponse,
    EntriesPage,
    TransferRequest,
    TransferResponse,
    WalletRequest,
    WalletResponse,
)
from app.services.response_cache import ResponseCache
from app.services.wallets import (
    create_wallet,
    get_wallet,
    transfer_between_wallets,
    wallet_balance,
    wallet_entries,
)

router = APIRouter(prefix="/v1")


async def _wallet_or_404(
    db: AsyncSession, tenant_id: uuid.UUID, wallet_id: uuid.UUID
) -> Wallet:
    wallet = await get_wallet(db, tenant_id=tenant_id, wallet_id=wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet {wallet_id} not found")
    return wallet


@router.post(
    "/wallets",
    response_model=WalletResponse,
    status_code=201,
    responses=IDEMPOTENT_ERRORS,
)
async def create_wallet_route(
    request: Request,
    payload: WalletRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db_dep),
    cache: ResponseCache = Depends(get_response_cache),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    response, _ = await idempotent_response(
        route="wallets",
        request=request,
        db=db,
        cache=cache,
        clock=clock,
        settings=settings,
        tenant_id=tenant_id,
        key=idempotency_key,
        handler=lambda: create_wallet(
            db, tenant_id=tenant_id, currency=payload.currency
        ),
    )
    return response


@router.get("/wallets/{wallet_id}/balance", response_model=BalanceResponse)
async def wallet_balance_route(
    wallet_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db_dep),
) -> dict[str, Any]:
    return await wallet_balance(db, await _wallet_or_404(db, tenant_id, wallet_id))


@router.get("/wallets/{wallet_id}/entries", response_model=EntriesPage)
async def wallet_entries_route(
    wallet_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    before: int | None = Query(default=None, ge=1),
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db_dep),
) -> dict[str, Any]:
    wallet = await _wallet_or_404(db, tenant_id, wallet_id)
    return await wallet_entries(db, wallet, limit=limit, before=before)


@router.post(
    "/transfers",
    response_model=TransferResponse,
    status_code=201,
    responses={**IDEMPOTENT_ERRORS, 404: {"description": "Wallet not found"}},
)
async def create_transfer_route(
    request: Request,
    payload: TransferRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant),
    idempotency_key: str = Depends(require_idempotency_key),
    db: AsyncSession = Depends(get_db_dep),
    cache: ResponseCache = Depends(get_response_cache),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    response, _ = await idempotent_response(
        route="transfers",
        request=request,
        db=db,
        cache=cache,
        clock=clock,
        settings=settings,
        tenant_id=tenant_id,
        key=idempotency_key,
        handler=lambda: transfer_between_wallets(
            db,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            from_wallet_id=payload.from_wallet_id,
            to_wallet_id=payload.to_wallet_id,
            amount=payload.amount,
        ),
    )
    return response
