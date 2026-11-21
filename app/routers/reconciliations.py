"""Reconciliation API (platform keys only).

POST runs the reconciliation of one provider file date synchronously and
returns the run summary. Re-running a date is safe: nothing is posted twice.
A run already in progress for the date is a 409. Large files belong to the
job (`python -m app.workers.recon_job --date ...`), not to a request.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import PspClient, PspUnavailable
from app.config import Settings
from app.core.clock import Clock, get_clock
from app.dependencies import (
    get_psp_client,
    get_session_factory,
    get_settings_dep,
    require_platform_tenant,
)
from app.models.reconciliation import (
    Classification,
    ReconItem,
    ReconRun,
    SettlementLine,
)
from app.schemas.reconciliation import (
    ReconciliationRequest,
    ReconciliationResponse,
    ReconItemResponse,
)
from app.services.reconciliation import (
    Reconciler,
    ReconInProgressError,
    SettlementFileError,
)

router = APIRouter(prefix="/v1")
ITEMS_LIMIT = 1000


def _run_body(
    run: ReconRun, items: list[ReconItemResponse] | None = None
) -> dict[str, Any]:
    return ReconciliationResponse(
        id=run.id,
        provider=run.provider,
        file_date=run.file_date,
        status=run.status.value,
        lines=run.lines,
        counts=run.counts,
        unexplained=run.unexplained,
        posted=run.posted,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
        items=items,
    ).model_dump(mode="json")


@router.post(
    "/reconciliations",
    status_code=201,
    response_model=ReconciliationResponse,
    responses={
        403: {"description": "Not a platform API key"},
        409: {"description": "A run for this date is in progress"},
        502: {"description": "The settlement file could not be fetched or parsed"},
    },
)
async def create_reconciliation(
    payload: ReconciliationRequest,
    _: uuid.UUID = Depends(require_platform_tenant),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    psp: PspClient = Depends(get_psp_client),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    reconciler = Reconciler(session_factory, psp, clock, settings)
    try:
        run = await reconciler.run(payload.date)
    except ReconInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (PspUnavailable, SettlementFileError) as exc:
        raise HTTPException(502, f"settlement file: {exc}") from exc
    return _run_body(run)


@router.get(
    "/reconciliations/{run_id}",
    response_model=ReconciliationResponse,
    responses={403: {"description": "Not a platform API key"}, 404: {}},
)
async def get_reconciliation(
    run_id: uuid.UUID,
    classification: Classification | None = Query(default=None),
    unexplained_only: bool = Query(default=False),
    _: uuid.UUID = Depends(require_platform_tenant),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> dict[str, Any]:
    async with session_factory() as db:
        run = await db.get(ReconRun, run_id)
        if run is None:
            raise HTTPException(404, f"reconciliation {run_id} not found")
        query = (
            select(ReconItem, SettlementLine.line_id)
            .outerjoin(
                SettlementLine, SettlementLine.id == ReconItem.settlement_line_id
            )
            .where(ReconItem.run_id == run_id)
            .order_by(ReconItem.classification, SettlementLine.line_id, ReconItem.id)
            .limit(ITEMS_LIMIT)
        )
        if classification is not None:
            query = query.where(ReconItem.classification == classification)
        if unexplained_only:
            query = query.where(
                ReconItem.classification.notin_(
                    [Classification.MATCHED, Classification.HEALED]
                )
            )
        rows = (await db.execute(query)).all()
    items = [
        ReconItemResponse(
            id=item.id,
            classification=item.classification.value,
            settlement_line_id=item.settlement_line_id,
            line_id=line_id,
            tenant_id=item.tenant_id,
            payment_id=item.payment_id,
            refund_id=item.refund_id,
            expected=item.expected,
            actual=item.actual,
            posted=item.posted,
            detail=item.detail,
            resolved=item.resolved,
        )
        for item, line_id in rows
    ]
    return _run_body(run, items)
