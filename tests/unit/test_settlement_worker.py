"""
Unit tests for app/workers/settlement_worker.py
Kafka consumer, DB session, and HTTP client are all mocked.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.payment import PaymentStatus


def make_kafka_message(
    payment_id="pay-1", topic="payment.initiated", partition=0, offset=1
) -> None:
    msg = MagicMock()
    msg.topic = topic
    msg.partition = partition
    msg.offset = offset
    msg.value = f'{{"payment_id": "{payment_id}", "amount": "50.00", "currency": "USD"}}'.encode()
    return msg


# process_message — success path


@pytest.mark.asyncio
async def test_process_message_transitions_to_settled() -> None:
    from app.workers.settlement_worker import process_message

    mock_db = AsyncMock()
    mock_db.add = MagicMock()

    # 1. Setup Mock DB response for the processed event check (returns None so it continues)
    mock_event_result = MagicMock()
    mock_event_result.scalar_one_or_none.return_value = None

    # 2. Setup Mock DB response for finding the payment
    mock_payment = MagicMock()
    mock_payment.id = "pay-1"
    mock_payment.status = (
        PaymentStatus.PENDING
    )  # Must be pending to transition to processing
    mock_payment.amount = Decimal("50.00")
    mock_payment.currency = "USD"

    mock_payment_result = MagicMock()
    mock_payment_result.scalar_one_or_none.return_value = mock_payment

    # Apply the DB responses in order
    mock_db.execute.side_effect = [mock_event_result, mock_payment_result]

    # Setup HTTP mock
    mock_http = AsyncMock()
    mock_http.post.return_value = MagicMock(status_code=200)

    # Setup Kafka Mock
    mock_kafka = AsyncMock()

    # Call the actual signature
    await process_message(mock_db, mock_http, mock_kafka, "pay-1", 0, "event-123")

    # Assert the internal state changed
    assert mock_payment.status == PaymentStatus.SETTLED
    mock_kafka.send_and_wait.assert_awaited_once()


# process_message — already processed (deduplication)


@pytest.mark.asyncio
async def test_process_message_skips_if_already_processed() -> None:
    from app.workers.settlement_worker import process_message

    mock_db = AsyncMock()
    mock_db.add = MagicMock()

    # Simulate DB returning an existing event (meaning it was already processed)
    mock_event_result = MagicMock()
    mock_event_result.scalar_one_or_none.return_value = MagicMock()
    mock_db.execute.return_value = mock_event_result

    mock_http = AsyncMock()
    mock_kafka = AsyncMock()

    await process_message(mock_db, mock_http, mock_kafka, "pay-1", 0, "event-123")

    # HTTP client should never be called if it was skipped
    mock_http.post.assert_not_called()


# process_message — provider failure increments retry count


@pytest.mark.asyncio
async def test_process_message_handles_provider_failure() -> None:
    from app.workers.settlement_worker import process_message

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_event_result = MagicMock()
    mock_event_result.scalar_one_or_none.return_value = None

    mock_payment = MagicMock()
    mock_payment.status = PaymentStatus.PENDING
    mock_payment_result = MagicMock()
    mock_payment_result.scalar_one_or_none.return_value = mock_payment

    mock_db.execute.side_effect = [mock_event_result, mock_payment_result]

    mock_http = AsyncMock()
    # Force HTTP provider to fail
    mock_http.post.side_effect = Exception("Provider timeout")

    mock_kafka = AsyncMock()

    # Disable sleep so the test runs instantly
    with patch("asyncio.sleep", new=AsyncMock()):
        await process_message(mock_db, mock_http, mock_kafka, "pay-1", 0, "event-123")

    # It should have caught the error, rolled back, and sent to Kafka for retry
    mock_db.rollback.assert_awaited_once()
    mock_kafka.send_and_wait.assert_awaited_once()


# Event ID is deterministic (same message → same UUID)


def test_event_id_is_deterministic() -> None:
    from app.workers.settlement_worker import compute_event_id

    id1 = compute_event_id("topicA", 0, 42)
    id2 = compute_event_id("topicA", 0, 42)

    assert id1 == id2


def test_event_id_differs_for_different_offsets() -> None:
    from app.workers.settlement_worker import compute_event_id

    id1 = compute_event_id("topicA", 0, 1)
    id2 = compute_event_id("topicA", 0, 2)

    assert id1 != id2
