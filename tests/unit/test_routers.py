"""
Unit tests for app/routers/payments.py and app/routers/webhooks.py
Uses httpx.AsyncClient with the FastAPI app — no running server needed.
All services are patched at the router boundary.
"""

import uuid
from datetime import UTC
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# App & Dependency Fixtures


@pytest.fixture
def mock_db():
    """Provides a fresh AsyncMock for the database per test."""
    return AsyncMock()


@pytest.fixture
def mock_redis():
    """Provides a fresh AsyncMock for Redis per test."""
    redis = AsyncMock()
    # Provide default safe returns to prevent json.loads() crashes
    redis.get.return_value = None
    redis.setnx.return_value = 1
    return redis


@pytest.fixture
def app(mock_db, mock_redis):
    from app.dependencies import get_db_dep, get_redis
    from app.main import app as fastapi_app

    # Inject mock state and override FastAPI dependencies
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


# POST /payments


@pytest.mark.asyncio
async def test_create_payment_returns_202(client) -> None:
    response_body = {
        "payment_id": "pay-abc",
        "status": "pending",
        "amount": "50.00",
        "currency": "USD",
    }
    with patch(
        "app.routers.payments.create_payment", new=AsyncMock(return_value=response_body)
    ):
        resp = await client.post(
            "/payments",
            json={"amount": 50.00, "currency": "USD"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    assert resp.status_code == 202
    assert resp.json()["payment_id"] == "pay-abc"


@pytest.mark.asyncio
async def test_create_payment_missing_idempotency_key_returns_422(client) -> None:
    resp = await client.post("/payments", json={"amount": 50.00, "currency": "USD"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_payment_missing_amount_returns_422(client) -> None:
    resp = await client.post(
        "/payments",
        json={"currency": "USD"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_payment_negative_amount_returns_422(client) -> None:
    resp = await client.post(
        "/payments",
        json={"amount": -10.00, "currency": "USD"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_payment_conflict_returns_409(client) -> None:
    with patch(
        "app.routers.payments.create_payment", new=AsyncMock(side_effect=ValueError())
    ):
        resp = await client.post(
            "/payments",
            json={"amount": 50.00, "currency": "USD"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    assert resp.status_code == 409


# GET /payments/{id}


@pytest.mark.asyncio
async def test_get_payment_returns_200(client, mock_db) -> None:
    from datetime import datetime
    from unittest.mock import MagicMock

    from app.models.payment import PaymentStatus

    mock_payment = MagicMock()
    mock_payment.id = "pay-abc"
    mock_payment.status = PaymentStatus.SETTLED
    mock_payment.amount = Decimal("50.00")
    mock_payment.currency = "USD"
    mock_payment.created_at = datetime.now(UTC)
    mock_payment.updated_at = datetime.now(UTC)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_payment
    mock_db.execute.return_value = mock_result

    test_id = str(uuid.uuid4())
    resp = await client.get(f"/payments/{test_id}")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_payment_not_found_returns_404(client, mock_db) -> None:
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    test_id = str(uuid.uuid4())
    resp = await client.get(f"/payments/{test_id}")

    assert resp.status_code == 404


# GET /health


@pytest.mark.asyncio
async def test_health_returns_200(client) -> None:
    # Patch the engine and Kafka clients
    with (
        patch("app.routers.health.engine") as mock_engine,
        patch("app.routers.health.AIOKafkaAdminClient") as mock_kafka_cls,
    ):
        # 1. AsyncMock automatically handles the `async with engine.connect()` context
        mock_engine.connect.return_value = AsyncMock()

        # 2. Mock the Kafka instance so `await admin.start()` is a valid coroutine
        mock_kafka_cls.return_value = AsyncMock()

        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# POST /webhooks


@pytest.mark.asyncio
async def test_webhook_settled_returns_200(client) -> None:
    resp = await client.post(
        "/webhooks",
        json={"transaction_id": "ref-123", "status": "settled"},
    )
    assert resp.status_code == 200
    assert resp.json()["received"] is True


@pytest.mark.asyncio
async def test_webhook_invalid_body_returns_422(client) -> None:
    resp = await client.post("/webhooks", json={})
    assert resp.status_code == 422
