from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class WebhookPayload(BaseModel):
    transaction_id: str
    status: str
    reason: str | None = None


@router.post("/webhooks", status_code=200)
async def receive_webhook(payload: WebhookPayload) -> dict[str, Any]:
    return {"received": True, "transaction_id": payload.transaction_id}
