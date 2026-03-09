"""
Unit tests for app/services/outbox_poller.py
DB and Kafka producer are mocked.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def make_outbox_event(
    event_id="evt-1", event_type="payment.initiated", published=False
) -> Any:
    evt = MagicMock()
    evt.id = event_id
    evt.event_type = event_type
    evt.payload = '{"payment_id": "pay-1"}'
    evt.published_at = None if not published else datetime.now(UTC)
    evt.created_at = datetime.now(UTC)
    return evt


# Returns 0 when outbox is empty


@pytest.mark.asyncio
async def test_poll_returns_zero_when_empty() -> None:
    from app.services.outbox_poller import poll_and_publish

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_producer = AsyncMock()

    # Simulate empty result from DB query
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    count, lag = await poll_and_publish(mock_db, mock_producer)

    assert count == 0
    assert lag == 0.0
    mock_producer.send_and_wait.assert_not_awaited()


# Publishes each unpublished event


@pytest.mark.asyncio
async def test_poll_publishes_all_unpublished() -> None:
    from app.services.outbox_poller import poll_and_publish

    events = [make_outbox_event(f"evt-{i}") for i in range(3)]

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_producer = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = events
    mock_db.execute.return_value = mock_result

    count, _ = await poll_and_publish(mock_db, mock_producer)

    assert count == 3
    assert mock_producer.send_and_wait.await_count == 3


# Sets published_at after successful publish


@pytest.mark.asyncio
async def test_poll_marks_published_at() -> None:
    from app.services.outbox_poller import poll_and_publish

    event = make_outbox_event()
    assert event.published_at is None

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_producer = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [event]
    mock_db.execute.return_value = mock_result

    await poll_and_publish(mock_db, mock_producer)

    assert event.published_at is not None


# Kafka failure — does NOT mark published_at, does not crash


@pytest.mark.asyncio
async def test_poll_continues_on_kafka_error() -> None:
    from app.services.outbox_poller import poll_and_publish

    event = make_outbox_event()

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_producer = AsyncMock()
    mock_producer.send_and_wait.side_effect = Exception("Kafka unavailable")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [event]
    mock_db.execute.return_value = mock_result

    # Should not raise
    count, _ = await poll_and_publish(mock_db, mock_producer)

    # Event not marked as published when Kafka fails
    assert event.published_at is None


# Returns lag in seconds for oldest unpublished event


@pytest.mark.asyncio
async def test_poll_returns_positive_lag_for_old_event() -> None:
    from app.services.outbox_poller import poll_and_publish

    old_event = make_outbox_event()
    old_event.created_at = datetime(2020, 1, 1, tzinfo=UTC)  # very old

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_producer = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [old_event]
    mock_db.execute.return_value = mock_result

    _, lag = await poll_and_publish(mock_db, mock_producer)

    assert lag > 0
