"""
Service-level tests for execute_idempotent / begin against real PostgreSQL:
the transaction semantics the HTTP tests cannot isolate.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.models.idempotency import IdempotencyKey
from app.models.outbox_event import OutboxEvent
from app.services.idempotency import (
    BeginOutcome,
    BusinessRejection,
    HandlerResult,
    IdempotentResult,
    ReplaySource,
    begin,
    execute_idempotent,
)
from app.services.response_cache import ResponseCache
from tests.conftest import DEFAULT_TENANT_ID

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _no_cache() -> ResponseCache:
    return ResponseCache(None, ttl_seconds=60, cooldown_seconds=1)


async def _run(
    db: AsyncSession,
    key: str,
    handler: Callable[[], Awaitable[HandlerResult]],
    fp: str = "fp",
) -> IdempotentResult:
    return await execute_idempotent(
        db,
        _no_cache(),
        tenant_id=DEFAULT_TENANT_ID,
        key=key,
        request_fingerprint=fp,
        handler=handler,
        clock=FakeClock(T0),
        ttl_seconds=3600,
        lock_timeout_ms=1000,
    )


async def _key_rows(db: AsyncSession, key: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(IdempotencyKey)
        .where(IdempotencyKey.key == key)
    )
    return int(result.scalar_one())


async def _outbox_rows(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(OutboxEvent))
    return int(result.scalar_one())


def _outbox_row() -> OutboxEvent:
    return OutboxEvent(
        id=uuid.uuid4(), tenant_id=DEFAULT_TENANT_ID, event_type="test", payload={}
    )


async def test_business_rejection_rolls_back_writes_but_keeps_the_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def rejecting_handler() -> HandlerResult:
        db.add(_outbox_row())
        await db.flush()
        raise BusinessRejection(422, {"detail": "insufficient funds"})

    async with session_factory() as db:
        result = await _run(db, "reject", rejecting_handler)
    assert result.response.status_code == 422

    async with session_factory() as check:
        assert await _outbox_rows(check) == 0
        row = (
            await check.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == "reject")
            )
        ).scalar_one()
        assert row.status == "completed"
        assert row.response_status == 422
        assert row.response_body == {"detail": "insufficient funds"}

    calls = 0

    async def must_not_run() -> HandlerResult:
        nonlocal calls
        calls += 1
        return HandlerResult(202, {})

    async with session_factory() as db:
        replay = await _run(db, "reject", must_not_run)
    assert calls == 0
    assert replay.source is ReplaySource.DATABASE
    assert replay.response.status_code == 422


async def test_unexpected_error_rolls_back_everything_including_the_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def failing_handler() -> HandlerResult:
        db.add(_outbox_row())
        await db.flush()
        raise RuntimeError("boom")

    async with session_factory() as db:
        with pytest.raises(RuntimeError):
            await _run(db, "crash", failing_handler)

    async with session_factory() as check:
        assert await _key_rows(check, "crash") == 0
        assert await _outbox_rows(check) == 0

    async def ok_handler() -> HandlerResult:
        return HandlerResult(202, {"ok": True})

    async with session_factory() as db:
        retry = await _run(db, "crash", ok_handler)
    assert retry.source is ReplaySource.NONE


async def test_expired_unswept_key_is_taken_over(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def first() -> HandlerResult:
        return HandlerResult(202, {"n": 1})

    async with session_factory() as db:
        await _run(db, "takeover", first, fp="fp-1")
    async with session_factory() as db:
        await db.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key == "takeover")
            .values(expires_at=T0 - timedelta(seconds=1))
        )
        await db.commit()

    async with session_factory() as db:
        started = await begin(
            db,
            tenant_id=DEFAULT_TENANT_ID,
            key="takeover",
            request_fingerprint="fp-2",
            now=T0,
            ttl_seconds=3600,
        )
        assert started.outcome is BeginOutcome.OWNED
        await db.rollback()


async def test_begin_reports_mismatch_and_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def handler() -> HandlerResult:
        return HandlerResult(201, {"id": "abc"})

    async with session_factory() as db:
        await _run(db, "k", handler, fp="fp-a")

    async with session_factory() as db:
        same = await begin(
            db,
            tenant_id=DEFAULT_TENANT_ID,
            key="k",
            request_fingerprint="fp-a",
            now=T0,
            ttl_seconds=60,
        )
        other = await begin(
            db,
            tenant_id=DEFAULT_TENANT_ID,
            key="k",
            request_fingerprint="fp-b",
            now=T0,
            ttl_seconds=60,
        )
        await db.rollback()
    assert same.outcome is BeginOutcome.REPLAY
    assert same.stored is not None and same.stored.body == {"id": "abc"}
    assert same.stored.status_code == 201
    assert other.outcome is BeginOutcome.MISMATCH


async def test_expiry_job_deletes_only_expired_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.workers.idempotency_expiry import run_once

    async def handler() -> HandlerResult:
        return HandlerResult(202, {})

    for key in ("a", "b", "c"):
        async with session_factory() as db:
            await _run(db, key, handler)
    async with session_factory() as db:
        await db.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key.in_(["a", "b"]))
            .values(expires_at=T0 - timedelta(minutes=1))
        )
        await db.commit()

    assert await run_once(session_factory, FakeClock(T0), batch_size=1) == 2
    async with session_factory() as check:
        remaining = (await check.execute(select(IdempotencyKey.key))).scalars().all()
    assert remaining == ["c"]
