import asyncio
import pytest
from app.services.idempotency import get_cached_response, cache_response, acquire_lock, release_lock
from app.services.payment_service import create_payment


async def test_idempotent_retry_returns_same_response(db_session, redis_client):
    """Same idempotency key twice → identical responses, only one DB row."""
    response1 = await create_payment(db_session, redis_client, "key-idem-001", 100.00, "USD")

    # second request — must return from cache, not re-execute
    response2 = await create_payment(db_session, redis_client, "key-idem-001", 100.00, "USD")

    assert response1["payment_id"] == response2["payment_id"]
    assert response1["status"] == response2["status"]

    # only one DB row
    from sqlalchemy import select, func
    from app.models.payment import Payment
    result = await db_session.execute(
        select(func.count()).where(Payment.idempotency_key == "key-idem-001")
    )
    assert result.scalar() == 1


async def test_concurrent_duplicate_requests(db_session, redis_client):
    """5 simultaneous requests with same key → only one payment row created."""
    tasks = [
        create_payment(db_session, redis_client, "key-concurrent-001", 100.00, "USD")
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # count successful responses
    successes = [r for r in results if isinstance(r, dict)]
    assert len(successes) >= 1

    # all successes return same payment_id
    payment_ids = {r["payment_id"] for r in successes}
    assert len(payment_ids) == 1

    # only one DB row
    from sqlalchemy import select, func
    from app.models.payment import Payment
    result = await db_session.execute(
        select(func.count()).where(Payment.idempotency_key == "key-concurrent-001")
    )
    assert result.scalar() == 1
