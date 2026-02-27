import random
import asyncio
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/charge")
async def charge(payload: dict):
    if random.random() < 0.1:
        await asyncio.sleep(31)
    if random.random() < 0.9:
        return {"status": "success", "transaction_id": "txn_123"}
    return {"status": "failed", "reason": "card_declined"}
