"""
Unit tests for app/services/idempotency.py (no database, no Redis).
Transaction behaviour is tested against real Postgres in
tests/integration/test_idempotency*.py.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.clock import FakeClock
from app.core.exceptions import IdempotencyKeyMismatchError
from app.services.idempotency import (
    BusinessRejection,
    HandlerResult,
    ReplaySource,
    execute_idempotent,
    fingerprint,
)
from app.services.response_cache import StoredResponse

TENANT = uuid.uuid4()

# fingerprint


def test_fingerprint_is_stable_across_key_order_and_whitespace() -> None:
    a = fingerprint("POST", "/v1/payments", {"amount": 10.5, "currency": "USD"})
    b = fingerprint("post", "/v1/payments", {"currency": "USD", "amount": 10.5})
    assert a == b
    assert len(a) == 64


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/v1/payments", {"amount": 10.51, "currency": "USD"}),
        ("POST", "/v1/payments", {"amount": 10.5, "currency": "EUR"}),
        ("POST", "/v1/refunds", {"amount": 10.5, "currency": "USD"}),
        ("PUT", "/v1/payments", {"amount": 10.5, "currency": "USD"}),
        ("POST", "/v1/payments", {"amount": 10.5, "currency": "USD", "x": 1}),
    ],
)
def test_fingerprint_changes_with_any_value(method: str, path: str, body: Any) -> None:
    base = fingerprint("POST", "/v1/payments", {"amount": 10.5, "currency": "USD"})
    assert fingerprint(method, path, body) != base


def test_fingerprint_handles_nested_bodies() -> None:
    a = fingerprint("POST", "/p", {"meta": {"b": 1, "a": [1, 2]}})
    b = fingerprint("POST", "/p", {"meta": {"a": [1, 2], "b": 1}})
    assert a == b
    assert a != fingerprint("POST", "/p", {"meta": {"a": [2, 1], "b": 1}})


def test_business_rejection_carries_response() -> None:
    exc = BusinessRejection(422, {"detail": "insufficient funds"})
    assert exc.status_code == 422
    assert exc.body == {"detail": "insufficient funds"}
    assert "insufficient funds" in str(exc)


# execute_idempotent: cache paths never touch the database


def _cache(hit: StoredResponse | None) -> MagicMock:
    cache = MagicMock()
    cache.get = AsyncMock(return_value=hit)
    cache.put = AsyncMock()
    return cache


async def _execute(db: Any, cache: Any, fp: str = "fp") -> Any:
    handler = AsyncMock(return_value=HandlerResult(202, {}))
    result = await execute_idempotent(
        db,
        cache,
        tenant_id=TENANT,
        key="k",
        request_fingerprint=fp,
        handler=handler,
        clock=FakeClock(),
        ttl_seconds=60,
        lock_timeout_ms=100,
    )
    return result, handler


@pytest.mark.asyncio
async def test_cache_hit_with_matching_fingerprint_replays_without_db() -> None:
    db = AsyncMock()
    hit = StoredResponse(202, {"payment_id": "p1"}, "fp")
    result, handler = await _execute(db, _cache(hit))

    assert result.source is ReplaySource.CACHE
    assert result.replayed
    assert result.response.body == {"payment_id": "p1"}
    handler.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_with_different_fingerprint_is_422() -> None:
    db = AsyncMock()
    hit = StoredResponse(202, {"payment_id": "p1"}, "other")
    with pytest.raises(IdempotencyKeyMismatchError):
        await _execute(db, _cache(hit))
    db.execute.assert_not_awaited()
