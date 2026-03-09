"""
Unit tests for app/services/payment_service.py
All external I/O (Redis, DB) is mocked.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Helpers


def make_mock_payment(payment_id="pay-123", status="pending") -> None:
    payment = MagicMock()
    payment.id = payment_id
    payment.status = status
    payment.amount = Decimal("50.00")
    payment.currency = "USD"
    payment.idempotency_key = "idem-key-1"
    return payment


# Cache hit — returns immediately without hitting DB


@pytest.mark.asyncio
async def test_create_payment_returns_cached_on_hit() -> None:
    cached = {
        "status_code": 202,
        "body": {"payment_id": "pay-999", "status": "pending"},
    }

    with (
        patch(
            "app.services.payment_service.get_cached_response",
            new=AsyncMock(return_value=cached),
        ),
        patch(
            "app.services.payment_service.acquire_lock", new=AsyncMock()
        ) as mock_lock,
        patch("app.services.payment_service.release_lock", new=AsyncMock()),
    ):
        from app.services.payment_service import create_payment

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_redis = AsyncMock()

        result = await create_payment(mock_db, mock_redis, "idem-key-1", 50.00, "USD")

        assert result["payment_id"] == "pay-999"
        mock_lock.assert_not_awaited()  # lock never acquired on cache hit


# Cache miss — proceeds through full creation flow


@pytest.mark.asyncio
async def test_create_payment_acquires_lock_on_miss() -> None:
    with (
        patch(
            "app.services.payment_service.get_cached_response",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.payment_service.acquire_lock",
            new=AsyncMock(return_value=True),
        ) as mock_lock,
        patch("app.services.payment_service.release_lock", new=AsyncMock()),
        patch("app.services.payment_service.cache_response", new=AsyncMock()),
    ):
        from app.services.payment_service import create_payment

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_redis = AsyncMock()

        await create_payment(mock_db, mock_redis, "idem-key-1", 50.00, "USD")
        mock_lock.assert_awaited_once()


# Lock not acquired — returns 409


@pytest.mark.asyncio
async def test_create_payment_returns_409_when_lock_fails() -> None:
    from app.core.exceptions import IdempotencyConflictError

    with (
        patch(
            "app.services.payment_service.get_cached_response",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.payment_service.acquire_lock",
            new=AsyncMock(return_value=False),
        ),
    ):
        from app.services.payment_service import create_payment

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_redis = AsyncMock()

        with pytest.raises(IdempotencyConflictError):
            await create_payment(mock_db, mock_redis, "idem-key-1", 50.00, "USD")


# Lock is always released — even on DB exception


@pytest.mark.asyncio
async def test_create_payment_releases_lock_on_db_error() -> None:
    with (
        patch(
            "app.services.payment_service.get_cached_response",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.payment_service.acquire_lock",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.payment_service.release_lock", new=AsyncMock()
        ) as mock_release,
    ):
        from app.services.payment_service import create_payment

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        # Force the database to throw an error during the transaction
        mock_db.commit.side_effect = Exception("DB error")
        mock_redis = AsyncMock()

        with pytest.raises(Exception, match="DB error"):
            await create_payment(mock_db, mock_redis, "idem-key-1", 50.00, "USD")

        # Even though it crashed, it must release the lock!
        mock_release.assert_awaited_once()


# Response is cached after successful creation


@pytest.mark.asyncio
async def test_create_payment_caches_response() -> None:
    with (
        patch(
            "app.services.payment_service.get_cached_response",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.payment_service.acquire_lock",
            new=AsyncMock(return_value=True),
        ),
        patch("app.services.payment_service.release_lock", new=AsyncMock()),
        patch(
            "app.services.payment_service.cache_response", new=AsyncMock()
        ) as mock_cache,
    ):
        from app.services.payment_service import create_payment

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_redis = AsyncMock()

        await create_payment(mock_db, mock_redis, "idem-key-1", 50.00, "USD")
        mock_cache.assert_awaited_once()
