"""
Unit tests for app/workers/settlement_worker.py
Kafka consumer, DB session, and HTTP client are all mocked.
"""

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


# process_message — the provider's body decides the outcome (fixes v1 D1)


def _success_mocks(
    provider_body: dict[str, str],
) -> tuple[AsyncMock, MagicMock, AsyncMock]:
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    no_event = MagicMock()
    no_event.scalar_one_or_none.return_value = None
    payment = MagicMock()
    payment.id = "pay-1"
    payment.status = PaymentStatus.PENDING
    payment.amount = 5000
    payment.currency = "USD"
    payment.wallet_id = "wallet-1"
    found = MagicMock()
    found.scalar_one_or_none.return_value = payment
    mock_db.execute.side_effect = [no_event, found]
    http = AsyncMock()
    response = MagicMock(status_code=200)
    response.json.return_value = provider_body
    http.post.return_value = response
    return mock_db, payment, http


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"status": "success", "transaction_id": "txn_1"}, "captured"),
        ({"status": "failed", "reason": "card_declined"}, "declined"),
    ],
)
async def test_process_message_applies_the_outcome_in_the_body(
    body: dict[str, str], expected: str
) -> None:
    from app.workers.settlement_worker import process_message

    mock_db, payment, http = _success_mocks(body)
    with patch(
        "app.workers.settlement_worker.apply_psp_result", new=AsyncMock()
    ) as apply:
        await process_message(mock_db, http, AsyncMock(), "pay-1", 0, "event-123")

    assert payment.status == PaymentStatus.PROCESSING  # set before the call
    outcome = apply.call_args.kwargs["outcome"]
    assert outcome.kind.value == expected
    assert apply.call_args.kwargs["source"] == "settlement_worker"
    mock_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_legacy_payment_is_skipped_before_any_provider_call() -> None:
    """A pre-ledger payment must never reach the provider: the capture could
    not be recorded, and each retry would charge again."""
    from app.workers.settlement_worker import process_message

    mock_db, payment, http = _success_mocks({"status": "success"})
    payment.wallet_id = None
    with patch(
        "app.workers.settlement_worker.apply_psp_result", new=AsyncMock()
    ) as apply:
        await process_message(mock_db, http, AsyncMock(), "pay-1", 0, "event-123")
    http.post.assert_not_called()
    apply.assert_not_called()
    assert payment.status == PaymentStatus.PENDING
    mock_db.commit.assert_awaited_once()  # dedup row recorded


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
