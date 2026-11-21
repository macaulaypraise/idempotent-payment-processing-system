"""Provider webhooks: verify, store, acknowledge (DESIGN 6.3).

The endpoint does no business work. It checks the HMAC signature over the
raw bytes (re-serialised JSON would not match), inserts the event into the
inbox with ON CONFLICT DO NOTHING, and returns 200 at once, for a new event
and a duplicate alike, so the provider stops redelivering. The webhook
processor applies stored events later.

Authentication is the signature, not an API key: the provider is one party
for the whole platform, and the tenant is resolved from the payment when the
event is processed.
"""

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock, get_clock
from app.core.metrics import webhooks_received_total
from app.core.security import SIGNATURE_HEADER, SignatureError, verify_webhook
from app.dependencies import get_db_dep, get_settings_dep
from app.models.webhook_event import WebhookEvent, WebhookStatus

logger = structlog.get_logger()
router = APIRouter(prefix="/v1")

PROVIDER = "psp"


def _reject(status: int, reason: str, result: str) -> JSONResponse:
    webhooks_received_total.labels(result=result).inc()
    logger.warning("webhook_rejected", reason=reason)
    return JSONResponse({"detail": reason}, status_code=status)


@router.post(
    "/webhooks/psp",
    responses={
        400: {"description": "Bad signature or malformed event; nothing stored"},
        413: {"description": "Body too large"},
    },
)
async def receive_psp_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_dep),
    settings: Settings = Depends(get_settings_dep),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    body = await request.body()
    if len(body) > settings.WEBHOOK_MAX_BODY_BYTES:
        return _reject(413, "body too large", "too_large")
    try:
        verify_webhook(
            settings.PSP_WEBHOOK_SECRET,
            body,
            request.headers.get(SIGNATURE_HEADER),
            now=clock.now().timestamp(),
            tolerance_seconds=settings.PSP_WEBHOOK_TOLERANCE_SECONDS,
        )
    except SignatureError as exc:
        return _reject(400, str(exc), "bad_signature")

    try:
        event: dict[str, Any] = json.loads(body)
        provider_event_id = str(event["id"])
        event_type = str(event["type"])
        if not isinstance(event.get("data"), dict):
            raise ValueError("data must be an object")
    except (ValueError, KeyError, TypeError) as exc:
        return _reject(400, f"malformed event: {exc}", "malformed")

    stored = (
        await db.execute(
            insert(WebhookEvent)
            .values(
                provider=PROVIDER,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payload=event,
                status=WebhookStatus.RECEIVED,
                attempts=0,
                next_attempt_at=clock.now(),
                received_at=clock.now(),
            )
            .on_conflict_do_nothing(index_elements=["provider", "provider_event_id"])
            .returning(WebhookEvent.id)
        )
    ).scalar_one_or_none()
    await db.commit()

    duplicate = stored is None
    webhooks_received_total.labels(result="duplicate" if duplicate else "stored").inc()
    return JSONResponse({"received": True, "duplicate": duplicate})
