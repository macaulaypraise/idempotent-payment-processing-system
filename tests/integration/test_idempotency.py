"""
Stage 1 exit gate: Postgres is the idempotency source of truth.

Runs the real app in-process against real PostgreSQL, with Redis either
working or unreachable. Requires: PostgreSQL + Redis (CI services or
`docker compose up -d postgres redis`).
"""

import asyncio
import json
import os
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.idempotency import IdempotencyKey
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.services.response_cache import ResponseCache
from tests.conftest import TEST_REDIS_URL, UNREACHABLE_REDIS_URL

pytestmark = pytest.mark.asyncio

CONCURRENCY = int(os.environ.get("IPPS_CONCURRENCY", "1000"))
REDIS_STATES = pytest.mark.parametrize(
    "redis_url", [TEST_REDIS_URL, UNREACHABLE_REDIS_URL], ids=["redis-up", "redis-down"]
)


async def _count(db: AsyncSession, model: Any, *where: Any) -> int:
    result = await db.execute(select(func.count()).select_from(model).where(*where))
    return int(result.scalar_one())


def _headers(creds: Any, key: str) -> dict[str, str]:
    return {**creds.headers, "Idempotency-Key": key}


# --- Exit gate 1: one key, N concurrent requests, Redis up and down ----------


@REDIS_STATES
async def test_concurrent_requests_with_one_key_create_one_payment(
    api_client: Any, make_tenant: Any, db_session: AsyncSession, redis_url: str
) -> None:
    creds = await make_tenant()
    key = f"gate-{uuid.uuid4()}"

    async with api_client(redis_url=redis_url) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/payments", json=creds.payment(), headers=_headers(creds, key)
                )
                for _ in range(CONCURRENCY)
            ]
        )

    statuses = Counter(r.status_code for r in responses)
    assert statuses == {202: CONCURRENCY}, statuses
    bodies = {json.dumps(r.json(), sort_keys=True) for r in responses}
    assert len(bodies) == 1, f"responses differ: {bodies}"
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in responses) == (
        CONCURRENCY - 1
    )

    tenant = Payment.tenant_id == creds.tenant_id
    assert await _count(db_session, Payment, tenant) == 1
    assert (
        await _count(db_session, OutboxEvent, OutboxEvent.tenant_id == creds.tenant_id)
        == 1
    )
    assert await _count(db_session, IdempotencyKey, IdempotencyKey.key == key) == 1


@REDIS_STATES
async def test_sequential_retry_replays_identical_response(
    api_client: Any, make_tenant: Any, db_session: AsyncSession, redis_url: str
) -> None:
    creds = await make_tenant()
    async with api_client(redis_url=redis_url) as client:
        first = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k1")
        )
        second = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k1")
        )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert await _count(db_session, Payment, Payment.tenant_id == creds.tenant_id) == 1


# --- Exit gate 4: same key, different body -> 422 ----------------------------


@REDIS_STATES
async def test_same_key_different_body_returns_422(
    api_client: Any, make_tenant: Any, db_session: AsyncSession, redis_url: str
) -> None:
    creds = await make_tenant()
    async with api_client(redis_url=redis_url) as client:
        ok = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k2")
        )
        changed = creds.payment(amount=99_900)
        bad = await client.post(
            "/v1/payments", json=changed, headers=_headers(creds, "k2")
        )

    assert ok.status_code == 202
    assert bad.status_code == 422
    assert "different request" in bad.json()["detail"]
    assert await _count(db_session, Payment, Payment.tenant_id == creds.tenant_id) == 1


async def test_fingerprint_ignores_key_order_and_whitespace(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    headers = {**_headers(creds, "k-order"), "Content-Type": "application/json"}
    w = creds.wallet_id
    async with api_client() as client:
        a = await client.post(
            "/v1/payments",
            content=f'{{"amount": 500, "currency": "USD", "wallet_id": "{w}"}}',
            headers=headers,
        )
        b = await client.post(
            "/v1/payments",
            content=f'{{ "wallet_id":"{w}", "currency":"USD","amount":500 }}',
            headers=headers,
        )
    assert a.status_code == b.status_code == 202
    assert a.json() == b.json()


async def test_mismatch_detected_on_cache_hit(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k-cache")
        )
        bad = await client.post(
            "/v1/payments",
            json=creds.payment(amount=100),
            headers=_headers(creds, "k-cache"),
        )
    assert bad.status_code == 422


# --- Header and auth contract -------------------------------------------------


async def test_missing_idempotency_key_returns_400(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        resp = await client.post(
            "/v1/payments", json=creds.payment(), headers=creds.headers
        )
    assert resp.status_code == 400


async def test_overlong_idempotency_key_returns_400(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        resp = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "x" * 256)
        )
    assert resp.status_code == 400


async def test_missing_or_unknown_api_key_returns_401(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        none = await client.post(
            "/v1/payments", json=creds.payment(), headers={"Idempotency-Key": "a"}
        )
        wrong = await client.post(
            "/v1/payments",
            json=creds.payment(),
            headers={"Idempotency-Key": "a", "Authorization": "Bearer ipps_nope"},
        )
    assert none.status_code == wrong.status_code == 401
    assert none.headers["WWW-Authenticate"] == "Bearer"


async def test_revoked_api_key_returns_401(
    api_client: Any, make_tenant: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from app.cli import revoke_api_key

    creds = await make_tenant()
    async with session_factory() as db:
        assert await revoke_api_key(db, creds.api_key[:12]) == 1
    async with api_client() as client:
        resp = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k")
        )
    assert resp.status_code == 401


# --- Exit gate 3: tenant isolation --------------------------------------------


async def test_cross_tenant_read_returns_404(api_client: Any, make_tenant: Any) -> None:
    alice, bob = await make_tenant("alice"), await make_tenant("bob")
    async with api_client() as client:
        created = await client.post(
            "/v1/payments", json=alice.payment(), headers=_headers(alice, "k")
        )
        payment_id = created.json()["payment_id"]
        own = await client.get(f"/v1/payments/{payment_id}", headers=alice.headers)
        other = await client.get(f"/v1/payments/{payment_id}", headers=bob.headers)
    assert own.status_code == 200
    assert other.status_code == 404


async def test_same_key_in_two_tenants_creates_two_payments(
    api_client: Any, make_tenant: Any
) -> None:
    alice, bob = await make_tenant("alice"), await make_tenant("bob")
    async with api_client() as client:
        a = await client.post(
            "/v1/payments", json=alice.payment(), headers=_headers(alice, "shared")
        )
        b = await client.post(
            "/v1/payments", json=bob.payment(), headers=_headers(bob, "shared")
        )
    assert a.status_code == b.status_code == 202
    assert a.json()["payment_id"] != b.json()["payment_id"]


# --- Lock timeout -> 409 --------------------------------------------------------


async def test_waiting_on_an_in_flight_key_times_out_with_409(
    api_client: Any, make_tenant: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Proves lock_timeout fires on the duplicate-key wait, and that the key
    is usable once the in-flight transaction goes away."""
    creds = await make_tenant()
    key = "k-inflight"
    now = datetime.now(UTC)
    async with session_factory() as holder:
        # An uncommitted owner: same INSERT the API does, never committed.
        await holder.execute(
            text(
                "INSERT INTO idempotency_keys "
                "(tenant_id, key, fingerprint, status, created_at, expires_at) "
                "VALUES (:t, :k, 'x', 'in_progress', :now, :exp)"
            ),
            {
                "t": creds.tenant_id,
                "k": key,
                "now": now,
                "exp": now + timedelta(days=1),
            },
        )
        async with api_client(IDEMPOTENCY_LOCK_TIMEOUT_MS=200) as client:
            blocked = await client.post(
                "/v1/payments", json=creds.payment(), headers=_headers(creds, key)
            )
        await holder.rollback()

    assert blocked.status_code == 409
    assert blocked.headers["Retry-After"] == "1"

    async with api_client() as client:
        after = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, key)
        )
    assert after.status_code == 202


# --- Stored responses are the truth (fixes v1 D13) ------------------------------


async def test_replay_after_cache_loss_is_identical_even_if_status_moved_on(
    api_client: Any,
    make_tenant: Any,
    db_session: AsyncSession,
    redis_client: Redis,
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        first = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k")
        )
    payment_id = uuid.UUID(first.json()["payment_id"])

    await redis_client.flushdb()
    await db_session.execute(
        update(Payment)
        .where(Payment.id == payment_id)
        .values(status=PaymentStatus.PROCESSING)
    )
    await db_session.commit()

    async with api_client() as client:
        replay = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k")
        )
    assert replay.json() == first.json()
    assert replay.json()["status"] == "pending"


async def test_response_cached_only_after_commit(
    api_client: Any, make_tenant: Any, redis_client: Redis
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        resp = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "k")
        )
    raw = await redis_client.get(ResponseCache.cache_key(creds.tenant_id, "k"))
    assert raw is not None
    cached = json.loads(raw)
    assert cached["body"] == resp.json()
    assert cached["status_code"] == 202


# --- Expiry -------------------------------------------------------------------


async def test_reusing_a_swept_key_is_rejected_not_duplicated(
    api_client: Any,
    make_tenant: Any,
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    """After expiry the key row is gone but the payment is not. The per-tenant
    unique constraint turns a reuse into a stored 422, never a second payment."""
    from app.core.clock import FakeClock
    from app.workers.idempotency_expiry import run_once

    creds = await make_tenant()
    async with api_client() as client:
        await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "old")
        )

    later = FakeClock(datetime.now(UTC) + timedelta(days=2))
    assert await run_once(session_factory, later, batch_size=10) == 1
    await redis_client.flushdb()

    async with api_client() as client:
        reuse = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "old")
        )
        again = await client.post(
            "/v1/payments", json=creds.payment(), headers=_headers(creds, "old")
        )
    assert reuse.status_code == again.status_code == 422
    assert reuse.json() == again.json()
    assert "expired" in reuse.json()["detail"]
    assert await _count(db_session, Payment, Payment.tenant_id == creds.tenant_id) == 1
    assert (
        await _count(db_session, OutboxEvent, OutboxEvent.tenant_id == creds.tenant_id)
        == 1
    )
