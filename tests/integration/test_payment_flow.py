import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.payment_service import create_payment
from app.models.payment import PaymentStatus
from app.services.idempotency import get_cached_response

@pytest.mark.asyncio
async def test_create_payment_returns_response():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setnx.return_value = True

    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    result = await create_payment(
        db=db,
        redis=redis,
        idempotency_key="test-key-123",
        amount=100.00,
        currency="USD"
    )

    assert result["status"] == PaymentStatus.PENDING.value
    assert result["currency"] == "USD"

@pytest.mark.asyncio
async def test_idempotent_retry_returns_cached_response():
    redis = AsyncMock()
    redis.get.return_value = '{"body": {"payment_id": "123", "status": "pending"}, "status_code": 202}'

    db = AsyncMock()

    result = await get_cached_response(redis, "test-key-123")
    assert result["body"]["payment_id"] == "123"

@pytest.mark.asyncio
async def test_duplicate_request_raises_when_lock_taken():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setnx.return_value = False

    db = AsyncMock()

    with pytest.raises(ValueError, match="Duplicate request in progress"):
        await create_payment(
            db=db,
            redis=redis,
            idempotency_key="test-key-123",
            amount=100.00,
            currency="USD"
        )
