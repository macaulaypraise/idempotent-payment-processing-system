import asyncio
import random
from typing import Any

from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/charge")
async def charge(payload: dict[str, Any]) -> dict[str, str]:
    if random.random() < 0.1:
        await asyncio.sleep(31)
    if random.random() < 0.9:
        return {"status": "success", "transaction_id": "txn_123"}
    return {"status": "failed", "reason": "card_declined"}
