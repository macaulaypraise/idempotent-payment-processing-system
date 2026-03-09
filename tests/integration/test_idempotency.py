"""
Integration tests for idempotency behaviour.
Requires: real PostgreSQL + Redis (CI sidecars or docker-compose.test.yml)

These tests call create_payment() directly against real infrastructure —
no HTTP layer, no mocks. This isolates the service logic from the router.
"""

import asyncio

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment
from app.services.payment_service import create_payment

# Test 1: Same key twice → identical response, one DB row


@pytest.mark.asyncio
async def test_idempotent_retry_returns_same_response(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Same idempotency key twice → identical responses, only one DB row."""
    response1 = await create_payment(
        db_session, redis_client, "key-idem-001", 100.00, "USD"
    )
    response2 = await create_payment(
        db_session, redis_client, "key-idem-001", 100.00, "USD"
    )

    assert response1["payment_id"] == response2["payment_id"]
    assert response1["status"] == response2["status"]

    result = await db_session.execute(
        select(func.count()).where(Payment.idempotency_key == "key-idem-001")
    )
    assert result.scalar() == 1


# Test 2: 5 concurrent requests with same key → one payment row created


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """5 simultaneous requests with same key → only one payment row created."""
    tasks = [
        create_payment(db_session, redis_client, "key-concurrent-001", 100.00, "USD")
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if isinstance(r, dict)]
    assert len(successes) >= 1

    payment_ids = {r["payment_id"] for r in successes}
    assert len(payment_ids) == 1, f"Duplicate charges detected: {payment_ids}"

    result = await db_session.execute(
        select(func.count()).where(Payment.idempotency_key == "key-concurrent-001")
    )
    assert result.scalar() == 1


# Test 3: Different keys → different payments (DB isolation check)


@pytest.mark.asyncio
async def test_different_keys_create_different_payments(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Each unique idempotency key must produce a distinct payment row."""
    resp1 = await create_payment(db_session, redis_client, "key-diff-001", 50.00, "USD")
    resp2 = await create_payment(db_session, redis_client, "key-diff-002", 50.00, "USD")

    assert resp1["payment_id"] != resp2["payment_id"]

    result = await db_session.execute(
        select(func.count())
        .select_from(Payment)
        .where(Payment.idempotency_key.in_(["key-diff-001", "key-diff-002"]))
    )
    assert result.scalar() == 2


# Test 4: Lock is released after payment creation (Redis state check)


@pytest.mark.asyncio
async def test_lock_released_after_payment_creation(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """After create_payment completes, the distributed lock key must not exist."""
    idem_key = "key-lock-check-001"
    await create_payment(db_session, redis_client, idem_key, 30.00, "USD")

    lock_key = f"lock:{idem_key}"
    lock_exists = await redis_client.exists(lock_key)
    assert lock_exists == 0, "Lock was not released after payment creation"


# Test 5: Cached response persists in Redis with correct structure


@pytest.mark.asyncio
async def test_cached_response_stored_in_redis(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """After creation, Redis must hold a cached response for the idempotency key."""
    import json

    idem_key = "key-cache-check-001"
    response = await create_payment(db_session, redis_client, idem_key, 20.00, "USD")

    # The cache key pattern used by idempotency.py
    cache_key = f"idempotency:{idem_key}"
    raw = await redis_client.get(cache_key)

    assert raw is not None, "No cached response found in Redis"
    cached = json.loads(raw)
    assert cached["body"]["payment_id"] == response["payment_id"]
    assert cached["status_code"] == 202
