"""PSP simulator HTTP API.

    POST /charges       Idempotency-Key required; replays return the original
    POST /refunds       Idempotency-Key required; full refunds only
    GET  /charges/{id}
    GET  /charges?merchant_reference=...
    GET  /ground-truth  everything actually charged and refunded
    GET|PUT /admin/faults, POST /admin/reset, GET /health
    GET  /admin/webhooks[?status=], POST /admin/webhooks/{id}/redeliver,
    POST /admin/webhooks/drain  deliver every pending webhook now

Every recorded charge and refund also produces a signed webhook (see
webhooks.py) when PSP_SIM_WEBHOOK_URL is set.

Faults apply only to the first request for a key. A replay is answered from
the store immediately, which is what makes charge-then-timeout recoverable:
the charge happened, the response was lost, asking again returns it.
"""

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from psp_sim.faults import FaultProfile, Faults
from psp_sim.store import Store
from psp_sim.webhooks import Sender, WebhookDispatcher


class ChargeRequest(BaseModel):
    amount: int = Field(gt=0, strict=True)
    currency: str = Field(min_length=3, max_length=3)
    merchant_reference: str = Field(min_length=1, max_length=128)


class RefundRequest(BaseModel):
    charge_id: str
    amount: int = Field(gt=0, strict=True)
    # The merchant's own refund id, echoed in webhooks. Metadata: it is not
    # part of the request fingerprint, so a retry from an older client that
    # did not send it still replays instead of failing with 422.
    merchant_reference: str | None = Field(default=None, max_length=128)


class FaultsRequest(BaseModel):
    decline_rate: float = 0.0
    slow_rate: float = 0.0
    slow_seconds: float = 0.5
    unavailable_rate: float = 0.0
    charge_then_timeout_rate: float = 0.0
    refund_fail_rate: float = 0.0
    refund_then_timeout_rate: float = 0.0
    webhook_drop_rate: float = 0.0
    webhook_duplicate_rate: float = 0.0
    webhook_delay_max_seconds: float = 0.0
    seed: int | None = None


def _fingerprint(body: BaseModel, exclude: set[str] | None = None) -> str:
    canonical = json.dumps(
        body.model_dump(exclude=exclude), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_app(
    db_path: str | None = None,
    response_timeout_seconds: float | None = None,
    faults: Faults | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    webhook_sender: Sender | None = None,
) -> FastAPI:
    store = Store(db_path or os.environ.get("PSP_SIM_DB_PATH", ":memory:"))
    faults = faults or Faults(seed=int(os.environ.get("PSP_SIM_SEED", "0")))
    hang = (
        response_timeout_seconds
        if response_timeout_seconds is not None
        else float(os.environ.get("PSP_SIM_RESPONSE_TIMEOUT_SECONDS", "30"))
    )
    dispatcher = WebhookDispatcher(
        store,
        faults,
        url=webhook_url or os.environ.get("PSP_SIM_WEBHOOK_URL") or None,
        secret=webhook_secret or os.environ.get("PSP_SIM_WEBHOOK_SECRET", ""),
        sender=webhook_sender,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
        task = asyncio.create_task(dispatcher.run()) if dispatcher.enabled else None
        yield
        if task is not None:
            task.cancel()

    app = FastAPI(title="PSP simulator", lifespan=lifespan)
    app.state.store = store
    app.state.faults = faults
    app.state.webhooks = dispatcher

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/charges")
    async def create_charge(
        body: ChargeRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        fingerprint = _fingerprint(body)
        existing = store.charge_by_key(idempotency_key)
        if existing is not None:
            return _replay(existing.fingerprint, fingerprint, existing.public())

        profile = faults.profile
        if faults.roll(profile.unavailable_rate):
            raise HTTPException(503, "provider unavailable")
        if faults.roll(profile.slow_rate):
            await asyncio.sleep(profile.slow_seconds)
        declined = faults.roll(profile.decline_rate)
        charge, created = store.insert_charge(
            key=idempotency_key,
            fingerprint=fingerprint,
            merchant_reference=body.merchant_reference,
            amount=body.amount,
            currency=body.currency,
            status="declined" if declined else "succeeded",
            decline_reason="card_declined" if declined else None,
        )
        if not created:  # a concurrent first request won the insert
            return _replay(charge.fingerprint, fingerprint, charge.public())
        # Recorded before the response, so a lost response still has a webhook
        dispatcher.enqueue(
            "charge.failed" if declined else "charge.succeeded", charge.public()
        )
        if faults.roll(profile.charge_then_timeout_rate):
            await asyncio.sleep(hang)  # the charge is recorded; the answer is late
        return JSONResponse(charge.public(), status_code=201)

    @app.post("/refunds")
    async def create_refund(
        body: RefundRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        fingerprint = _fingerprint(body, exclude={"merchant_reference"})
        existing = store.refund_by_key(idempotency_key)
        if existing is not None:
            return _replay(existing.fingerprint, fingerprint, existing.public())

        charge = store.charge_by_id(body.charge_id)
        if charge is None:
            raise HTTPException(404, f"charge {body.charge_id} not found")
        if body.amount != charge.amount:
            raise HTTPException(422, "only full refunds are supported")

        profile = faults.profile
        if faults.roll(profile.unavailable_rate):
            raise HTTPException(503, "provider unavailable")
        if faults.roll(profile.slow_rate):
            await asyncio.sleep(profile.slow_seconds)
        if charge.status != "succeeded":
            status, reason = "failed", "charge_not_succeeded"
        elif store.succeeded_refund_for(charge.id) is not None:
            status, reason = "failed", "already_refunded"
        elif faults.roll(profile.refund_fail_rate):
            status, reason = "failed", "refund_rejected"
        else:
            status, reason = "succeeded", None
        refund, created = store.insert_refund(
            key=idempotency_key,
            fingerprint=fingerprint,
            charge_id=charge.id,
            amount=body.amount,
            status=status,
            failure_reason=reason,
            merchant_reference=body.merchant_reference,
        )
        if not created:
            return _replay(refund.fingerprint, fingerprint, refund.public())
        dispatcher.enqueue(f"refund.{status}", refund.public())
        if faults.roll(profile.refund_then_timeout_rate):
            await asyncio.sleep(hang)
        return JSONResponse(refund.public(), status_code=201)

    @app.get("/charges/{charge_id}")
    async def get_charge(charge_id: str) -> dict[str, Any]:
        charge = store.charge_by_id(charge_id)
        if charge is None:
            raise HTTPException(404, f"charge {charge_id} not found")
        return charge.public()

    @app.get("/charges")
    async def find_charges(merchant_reference: str = Query()) -> list[dict[str, Any]]:
        return [c.public() for c in store.charges_by_reference(merchant_reference)]

    @app.get("/ground-truth")
    async def ground_truth() -> dict[str, list[dict[str, Any]]]:
        return store.ground_truth()

    @app.get("/admin/faults")
    async def get_faults() -> dict[str, Any]:
        return {**faults.profile.__dict__, "seed": faults.seed}

    @app.put("/admin/faults")
    async def set_faults(body: FaultsRequest) -> dict[str, Any]:
        settings = body.model_dump()
        seed = settings.pop("seed")
        try:
            faults.configure(FaultProfile(**settings), seed=seed)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**faults.profile.__dict__, "seed": faults.seed}

    @app.get("/admin/webhooks")
    async def list_webhooks(status: str | None = None) -> list[dict[str, Any]]:
        return [w.public() for w in store.webhooks(status)]

    @app.post("/admin/webhooks/{webhook_id}/redeliver")
    async def redeliver(webhook_id: str) -> dict[str, Any]:
        if not dispatcher.redeliver(webhook_id):
            raise HTTPException(404, f"webhook {webhook_id} not found")
        return {"status": "pending", "id": webhook_id}

    @app.post("/admin/webhooks/drain")
    async def drain_webhooks() -> dict[str, int]:
        return {"accepted": await dispatcher.drain()}

    @app.post("/admin/reset")
    async def reset() -> dict[str, str]:
        store.reset()
        faults.configure(FaultProfile(), seed=faults.seed)
        return {"status": "reset"}

    return app


def _replay(stored: str, presented: str, body: dict[str, Any]) -> JSONResponse:
    if stored != presented:
        raise HTTPException(422, "Idempotency-Key reused with a different request")
    return JSONResponse(body, status_code=200, headers={"Idempotent-Replayed": "true"})


app = create_app()
