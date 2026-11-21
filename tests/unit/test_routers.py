"""
Unit tests for app/routers (services patched, no database).
HTTP behaviour against a real database is in tests/integration.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import IdempotencyConflictError, IdempotencyKeyMismatchError
from app.models.payment import Payment, PaymentStatus
from app.services.idempotency import IdempotentResult, ReplaySource
from app.services.response_cache import StoredResponse

TENANT = uuid.uuid4()
BODY = {"amount": 5000, "currency": "USD", "wallet_id": str(uuid.uuid4())}
AUTH = {"Authorization": "Bearer ipps_test"}


@pytest.fixture
def app() -> Any:
    from app.dependencies import (
        get_current_tenant,
        get_db_dep,
        get_response_cache,
    )
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_db_dep] = lambda: AsyncMock()
    fastapi_app.dependency_overrides[get_response_cache] = lambda: MagicMock()
    fastapi_app.dependency_overrides[get_current_tenant] = lambda: TENANT
    fastapi_app.state.redis = AsyncMock()
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def _result(source: ReplaySource, status: int = 202) -> IdempotentResult:
    return IdempotentResult(
        StoredResponse(status, {"payment_id": "pay-abc", "status": "pending"}, "fp"),
        source,
    )


def _post(client: AsyncClient, key: str | None = "k1", body: Any = BODY) -> Any:
    headers = dict(AUTH)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/v1/payments", json=body, headers=headers)


# POST /v1/payments


@pytest.mark.asyncio
async def test_create_payment_returns_stored_status_and_body(
    client: AsyncClient,
) -> None:
    with patch(
        "app.routers.idempotent.execute_idempotent",
        new=AsyncMock(return_value=_result(ReplaySource.NONE)),
    ):
        resp = await _post(client)
    assert resp.status_code == 202
    assert resp.json()["payment_id"] == "pay-abc"
    assert "Idempotent-Replayed" not in resp.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [ReplaySource.DATABASE, ReplaySource.CACHE])
async def test_replay_sets_header(client: AsyncClient, source: ReplaySource) -> None:
    with patch(
        "app.routers.idempotent.execute_idempotent",
        new=AsyncMock(return_value=_result(source)),
    ):
        resp = await _post(client)
    assert resp.headers["Idempotent-Replayed"] == "true"


@pytest.mark.asyncio
async def test_stored_rejection_is_returned_verbatim(client: AsyncClient) -> None:
    with patch(
        "app.routers.idempotent.execute_idempotent",
        new=AsyncMock(return_value=_result(ReplaySource.NONE, status=422)),
    ):
        resp = await _post(client)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_idempotency_key_returns_400(client: AsyncClient) -> None:
    resp = await _post(client, key=None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_blank_idempotency_key_returns_400(client: AsyncClient) -> None:
    resp = await _post(client, key="   ")
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"currency": "USD", "wallet_id": str(uuid.uuid4())},
        {"amount": 1000, "currency": "USD"},  # wallet_id is required
        {"amount": -1000, "currency": "USD"},
        {"amount": 0, "currency": "USD"},
        {"amount": 10.5, "currency": "USD"},  # floats are rejected outright
        {"amount": "1000", "currency": "USD"},  # strict: no string coercion
        {"amount": 10**15 + 1, "currency": "USD"},
        {"amount": 1000, "currency": "usd"},
        {"amount": 1000, "currency": "US"},
    ],
)
async def test_invalid_body_returns_422(client: AsyncClient, body: Any) -> None:
    resp = await _post(client, body=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_conflict_returns_409_with_retry_after(client: AsyncClient) -> None:
    with patch(
        "app.routers.idempotent.execute_idempotent",
        new=AsyncMock(side_effect=IdempotencyConflictError()),
    ):
        resp = await _post(client)
    assert resp.status_code == 409
    assert resp.headers["Retry-After"] == "1"


@pytest.mark.asyncio
async def test_mismatch_returns_422(client: AsyncClient) -> None:
    with patch(
        "app.routers.idempotent.execute_idempotent",
        new=AsyncMock(side_effect=IdempotencyKeyMismatchError()),
    ):
        resp = await _post(client)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401(app: Any) -> None:
    from app.dependencies import get_current_tenant

    del app.dependency_overrides[get_current_tenant]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/v1/payments", json=BODY, headers={"Idempotency-Key": "k"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unversioned_path_is_gone(client: AsyncClient) -> None:
    resp = await client.post(
        "/payments", json=BODY, headers={**AUTH, "Idempotency-Key": "k"}
    )
    assert resp.status_code == 404


# GET /v1/payments/{id}


@pytest.mark.asyncio
async def test_get_payment_returns_200(client: AsyncClient) -> None:
    payment = Payment(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        idempotency_key="k",
        amount=5000,
        currency="USD",
        status=PaymentStatus.SETTLED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with patch(
        "app.routers.payments.get_payment", new=AsyncMock(return_value=payment)
    ) as get:
        resp = await client.get(f"/v1/payments/{payment.id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "settled"
    assert get.call_args.kwargs["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_get_payment_not_found_returns_404(client: AsyncClient) -> None:
    with patch("app.routers.payments.get_payment", new=AsyncMock(return_value=None)):
        resp = await client.get(f"/v1/payments/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_payment_with_malformed_id_returns_422(client: AsyncClient) -> None:
    resp = await client.get("/v1/payments/not-a-uuid", headers=AUTH)
    assert resp.status_code == 422


# GET /health


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    with (
        patch("app.routers.health.engine") as mock_engine,
        patch("app.routers.health.AIOKafkaAdminClient") as mock_kafka_cls,
    ):
        mock_engine.connect.return_value = AsyncMock()
        mock_kafka_cls.return_value = AsyncMock()
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# POST /v1/webhooks (placeholder until Stage 5)


@pytest.mark.asyncio
async def test_webhook_settled_returns_200(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/webhooks", json={"transaction_id": "ref-123", "status": "settled"}
    )
    assert resp.status_code == 200
    assert resp.json()["received"] is True


@pytest.mark.asyncio
async def test_webhook_invalid_body_returns_422(client: AsyncClient) -> None:
    resp = await client.post("/v1/webhooks", json={})
    assert resp.status_code == 422
