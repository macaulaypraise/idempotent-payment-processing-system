"""Postgres-backed idempotency (ADR 0001, DESIGN section 6.1).

The key row is inserted in the same transaction as the business write. The
primary key (tenant_id, key) is the lock: a concurrent duplicate's INSERT
waits on it until the first transaction commits (then replays) or rolls back
(then owns the key). Redis only caches completed responses.

Flow, in one transaction:
  1. SET LOCAL lock_timeout
  2. INSERT key row ON CONFLICT DO NOTHING
  3. inserted   -> run the business handler inside a savepoint, store the
                   response on the key row, commit, then cache
     conflict   -> same fingerprint: replay the stored response
                   different fingerprint: 422
     lock wait exceeded lock_timeout -> 409 with Retry-After
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.db_errors import LOCK_NOT_AVAILABLE, pg_sqlstate
from app.core.exceptions import IdempotencyConflictError, IdempotencyKeyMismatchError
from app.models.idempotency import IdempotencyKey
from app.services.response_cache import ResponseCache, StoredResponse

_MAX_BEGIN_ATTEMPTS = 3


def fingerprint(method: str, path: str, body: Any) -> str:
    """SHA-256 of method, path and canonical JSON (sorted keys, no spaces)."""
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    material = f"{method.upper()}\n{path}\n{canonical}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HandlerResult:
    status_code: int
    body: dict[str, Any]
    resource_id: uuid.UUID | None = None


class BusinessRejection(Exception):  # noqa: N818 - a result, not a fault
    """A deterministic rejection (e.g. insufficient funds).

    Raised by a handler; its writes are rolled back to the savepoint, but the
    key row is kept and stores this response, so a retry gets the same answer.
    """

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("detail", "rejected"))
        self.status_code = status_code
        self.body = body


class BeginOutcome(Enum):
    OWNED = "owned"
    REPLAY = "replay"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class BeginResult:
    outcome: BeginOutcome
    stored: StoredResponse | None = None


class ReplaySource(Enum):
    NONE = "none"  # this request did the work
    DATABASE = "database"
    CACHE = "cache"


@dataclass(frozen=True)
class IdempotentResult:
    response: StoredResponse
    source: ReplaySource

    @property
    def replayed(self) -> bool:
        return self.source is not ReplaySource.NONE


def _stored_from_row(row: Any) -> StoredResponse:
    return StoredResponse(
        status_code=int(row.response_status),
        body=dict(row.response_body),
        fingerprint=str(row.fingerprint),
    )


async def set_lock_timeout(db: AsyncSession, milliseconds: int) -> None:
    """Transaction-scoped (SET LOCAL semantics); must run inside the transaction."""
    await db.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": f"{int(milliseconds)}ms"},
    )


async def begin(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    request_fingerprint: str,
    now: datetime,
    ttl_seconds: int,
) -> BeginResult:
    """Claim the key, or report what an earlier request with it produced.

    Runs inside the caller's transaction. May block until a concurrent owner
    of the same key commits or rolls back (bounded by lock_timeout).
    """
    expires_at = now + timedelta(seconds=ttl_seconds)
    for _ in range(_MAX_BEGIN_ATTEMPTS):
        inserted = await db.execute(
            insert(IdempotencyKey)
            .values(
                tenant_id=tenant_id,
                key=key,
                fingerprint=request_fingerprint,
                status="in_progress",
                created_at=now,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
            .returning(IdempotencyKey.key)
        )
        if inserted.scalar_one_or_none() is not None:
            return BeginResult(BeginOutcome.OWNED)

        row = (
            await db.execute(
                select(
                    IdempotencyKey.fingerprint,
                    IdempotencyKey.status,
                    IdempotencyKey.response_status,
                    IdempotencyKey.response_body,
                    IdempotencyKey.expires_at,
                ).where(
                    IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key
                )
            )
        ).one_or_none()
        if row is None:
            continue  # deleted by the expiry job between INSERT and SELECT

        if row.expires_at <= now:
            # Expired but not yet swept: take it over. Concurrent takeovers
            # serialise on the row lock; the loser re-reads the winner's row.
            took = await db.execute(
                update(IdempotencyKey)
                .where(
                    IdempotencyKey.tenant_id == tenant_id,
                    IdempotencyKey.key == key,
                    IdempotencyKey.expires_at <= now,
                )
                .values(
                    fingerprint=request_fingerprint,
                    status="in_progress",
                    response_status=None,
                    response_body=None,
                    resource_id=None,
                    created_at=now,
                    expires_at=expires_at,
                )
                .returning(IdempotencyKey.key)
            )
            if took.scalar_one_or_none() is not None:
                return BeginResult(BeginOutcome.OWNED)
            continue

        if row.status != "completed":
            # Only the owning transaction can see an in_progress row; seeing
            # one here means a writer committed without completing it.
            raise RuntimeError(f"idempotency key {key!r} committed while in_progress")

        if row.fingerprint != request_fingerprint:
            return BeginResult(BeginOutcome.MISMATCH)
        return BeginResult(BeginOutcome.REPLAY, _stored_from_row(row))

    raise IdempotencyConflictError()


async def complete(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    response: StoredResponse,
    resource_id: uuid.UUID | None,
) -> None:
    """Store the response on the key row, inside the caller's transaction."""
    await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key)
        .values(
            status="completed",
            response_status=response.status_code,
            response_body=response.body,
            resource_id=resource_id,
        )
    )


async def execute_idempotent(
    db: AsyncSession,
    cache: ResponseCache,
    *,
    tenant_id: uuid.UUID,
    key: str,
    request_fingerprint: str,
    handler: Callable[[], Awaitable[HandlerResult]],
    clock: Clock,
    ttl_seconds: int,
    lock_timeout_ms: int,
) -> IdempotentResult:
    """Run `handler` at most once per (tenant, key) and replay its response."""
    cached = await cache.get(tenant_id, key)
    if cached is not None:
        if cached.fingerprint != request_fingerprint:
            raise IdempotencyKeyMismatchError()
        return IdempotentResult(cached, ReplaySource.CACHE)

    try:
        await set_lock_timeout(db, lock_timeout_ms)
        started = await begin(
            db,
            tenant_id=tenant_id,
            key=key,
            request_fingerprint=request_fingerprint,
            now=clock.now(),
            ttl_seconds=ttl_seconds,
        )

        if started.outcome is BeginOutcome.MISMATCH:
            await db.rollback()
            raise IdempotencyKeyMismatchError()

        if started.outcome is BeginOutcome.REPLAY:
            await db.rollback()
            assert started.stored is not None
            await cache.put(tenant_id, key, started.stored)
            return IdempotentResult(started.stored, ReplaySource.DATABASE)

        try:
            async with db.begin_nested():
                result = await handler()
        except BusinessRejection as rejection:
            result = HandlerResult(rejection.status_code, rejection.body, None)

        response = StoredResponse(
            status_code=result.status_code,
            body=result.body,
            fingerprint=request_fingerprint,
        )
        await complete(
            db,
            tenant_id=tenant_id,
            key=key,
            response=response,
            resource_id=result.resource_id,
        )
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        if pg_sqlstate(exc) == LOCK_NOT_AVAILABLE:
            raise IdempotencyConflictError() from exc
        raise
    except BaseException:
        await db.rollback()
        raise

    # Cache only after commit: caching earlier could serve a response for a
    # transaction that then rolled back.
    await cache.put(tenant_id, key, response)
    return IdempotentResult(response, ReplaySource.NONE)


async def delete_expired_keys(
    db: AsyncSession, *, now: datetime, batch_size: int
) -> int:
    """Delete up to `batch_size` expired key rows; returns how many were deleted."""
    result = await db.execute(
        text(
            """
            DELETE FROM idempotency_keys
            WHERE (tenant_id, key) IN (
                SELECT tenant_id, key FROM idempotency_keys
                WHERE expires_at <= :now
                ORDER BY expires_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            """
        ),
        {"now": now, "limit": batch_size},
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
