"""
Unit tests for the full payment flow.
Services are mocked to ensure zero dependency on external infrastructure.
"""

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Fixtures


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setnx.return_value = 1
    return redis


@pytest.fixture
def app(mock_db, mock_redis):
    from app.dependencies import get_db_dep, get_redis
    from app.main import app as fastapi_app

    fastapi_app.state.redis = mock_redis
    fastapi_app.dependency_overrides[get_redis] = lambda: mock_redis
    fastapi_app.dependency_overrides[get_db_dep] = lambda: mock_db
    return fastapi_app


@pytest_asyncio.fixture
async def client(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# Test 1: Idempotent retry returns same response (core requirement)


@pytest.mark.asyncio
async def test_idempotent_retry_returns_same_response(client, mock_redis) -> None:
    key = str(uuid.uuid4())
    body = {"amount": 75.00, "currency": "USD"}

    # 1. First call: standard success
    resp1 = await client.post("/payments", json=body, headers={"Idempotency-Key": key})
    assert resp1.status_code == 202

    # 2. Mock Redis to act as if it cached the first response
    cached_data = {"body": resp1.json(), "status_code": 202}
    mock_redis.get.return_value = json.dumps(cached_data).encode()

    # 3. Second call: should hit the cache
    resp2 = await client.post("/payments", json=body, headers={"Idempotency-Key": key})
    assert resp2.status_code == 202
    assert resp1.json()["payment_id"] == resp2.json()["payment_id"]


# Test 2: Concurrent duplicate requests — only one payment created


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests(client, mock_redis) -> None:
    import asyncio

    key = str(uuid.uuid4())
    body = {"amount": 100.00, "currency": "USD"}

    # Simulate concurrency: only the very first request acquires the lock (returns 1).
    # The others fail to acquire it (return 0)
    mock_redis.setnx.side_effect = [1, 0, 0, 0, 0]

    responses = await asyncio.gather(
        *[
            client.post("/payments", json=body, headers={"Idempotency-Key": key})
            for _ in range(5)
        ]
    )

    success = [r for r in responses if r.status_code == 202]
    conflicts = [r for r in responses if r.status_code == 409]

    assert len(success) == 1
    assert len(conflicts) == 4


# Test 3: Different keys create different payments


@pytest.mark.asyncio
async def test_different_keys_create_different_payments(client, mock_redis) -> None:
    # Ensure fresh keys for both calls
    mock_redis.setnx.return_value = 1
    mock_redis.get.return_value = None

    resp1 = await client.post(
        "/payments",
        json={"amount": 50.00, "currency": "USD"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    resp2 = await client.post(
        "/payments",
        json={"amount": 50.00, "currency": "USD"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert resp1.json()["payment_id"] != resp2.json()["payment_id"]


# Test 4: GET /payments/{id} returns correct status


@pytest.mark.asyncio
async def test_get_payment_returns_created_payment(client, mock_db) -> None:
    resp = await client.post(
        "/payments",
        json={"amount": 25.00, "currency": "USD"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    payment_id = resp.json()["payment_id"]

    # Setup the DB mock to return a record
    mock_payment = MagicMock()
    mock_payment.id = uuid.UUID(payment_id)
    mock_payment.status.value = "pending"
    mock_payment.amount = Decimal("25.00")
    mock_payment.currency = "USD"
    mock_payment.created_at = "2023-01-01T00:00:00Z"
    mock_payment.updated_at = "2023-01-01T00:00:00Z"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_payment
    mock_db.execute.return_value = mock_result

    get_resp = await client.get(f"/payments/{payment_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["payment_id"] == payment_id


# Test 5: GET /payments/{id} returns 404 for unknown ID


@pytest.mark.asyncio
async def test_get_payment_unknown_id_returns_404(client, mock_db) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    resp = await client.get(f"/payments/{uuid.uuid4()}")
    assert resp.status_code == 404


# Test 6: Outbox event created alongside payment


@pytest.mark.asyncio
async def test_outbox_event_created_with_payment(client, mock_db) -> None:
    resp = await client.post(
        "/payments",
        json={"amount": 60.00, "currency": "USD"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 202

    # Verify that an OutboxEvent was added to the DB session
    from app.models.outbox_event import OutboxEvent

    added_objects = [call[0][0] for call in mock_db.add.call_args_list]
    outbox_events = [obj for obj in added_objects if isinstance(obj, OutboxEvent)]

    assert len(outbox_events) == 1
    assert resp.json()["payment_id"] in outbox_events[0].payload["payment_id"]


# Test 7: Outbox event marked published after poll


@pytest.mark.asyncio
async def test_outbox_published_after_poll(mock_db) -> None:
    from datetime import UTC, datetime

    from app.models.outbox_event import OutboxEvent
    from app.services.outbox_poller import poll_and_publish

    # Simulate DB returning 1 unpublished event
    mock_event = MagicMock(spec=OutboxEvent)
    mock_event.created_at = datetime.now(UTC)
    mock_event.published_at = None
    mock_event.event_type = "payment.initiated"
    mock_event.payload = {"payment_id": "123"}

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_event]
    mock_db.execute.return_value = mock_result

    mock_producer = AsyncMock()

    count, lag = await poll_and_publish(mock_db, mock_producer)

    assert count == 1
    mock_producer.send_and_wait.assert_awaited_once()
    assert mock_event.published_at is not None
    mock_db.commit.assert_awaited_once()
