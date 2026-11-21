"""
Unit tests for app/services/outbox_poller.py (DB and Kafka producer mocked).
Ordering against a real database, and the relay itself, are in
tests/integration/test_outbox_relay.py.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.outbox_poller import (
    DLQ_TOPIC,
    LEDGER_TOPIC,
    PAYMENTS_TOPIC,
    key_for,
    poll_and_publish,
    topic_for,
    value_for,
)


def make_event(
    seq: int, event_type: str = "payment.initiated", payload: Any = None
) -> Any:
    evt = MagicMock()
    evt.id = uuid.UUID(int=seq)
    evt.seq = seq
    evt.event_type = event_type
    evt.payload = payload if payload is not None else {"payment_id": f"pay-{seq}"}
    evt.published_at = None
    evt.created_at = datetime.now(UTC)
    return evt


def db_returning(events: list[Any]) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = events
    db.execute.return_value = result
    return db


async def test_empty_outbox_publishes_nothing() -> None:
    producer = AsyncMock()
    result = await poll_and_publish(db_returning([]), producer)
    assert result == (0, 0.0, False)
    producer.send_and_wait.assert_not_awaited()


async def test_publishes_every_row_in_the_order_given() -> None:
    events = [make_event(i) for i in (1, 2, 3)]
    producer = AsyncMock()
    result = await poll_and_publish(db_returning(events), producer)
    assert result.published == 3 and not result.failed
    keys = [c.kwargs["key"] for c in producer.send_and_wait.await_args_list]
    assert keys == [b"pay-1", b"pay-2", b"pay-3"]
    assert all(e.published_at is not None for e in events)


async def test_stops_at_the_first_failure_and_does_not_skip_past_it() -> None:
    """v1 skipped a failed row and published later ones, reordering a
    payment's events. Now the batch stops; nothing after the failure is sent."""
    events = [make_event(i) for i in (1, 2, 3)]
    producer = AsyncMock()
    producer.send_and_wait.side_effect = [None, Exception("broker down"), None]
    db = db_returning(events)

    result = await poll_and_publish(db, producer)

    assert result.published == 1 and result.failed
    assert producer.send_and_wait.await_count == 2
    assert events[0].published_at is not None
    assert events[1].published_at is None and events[2].published_at is None
    db.commit.assert_awaited_once()  # what was sent is still recorded


async def test_lag_is_the_age_of_the_oldest_row() -> None:
    old = make_event(1)
    old.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    result = await poll_and_publish(db_returning([old]), AsyncMock())
    assert result.lag > 0


async def test_headers_carry_event_id_and_type() -> None:
    event = make_event(7, "refund.created", {"payment_id": "pay-7", "refund_id": "r"})
    producer = AsyncMock()
    await poll_and_publish(db_returning([event]), producer)
    call = producer.send_and_wait.await_args
    assert call.args[0] == PAYMENTS_TOPIC
    assert dict(call.kwargs["headers"]) == {
        "event_id": str(event.id).encode(),
        "event_type": b"refund.created",
    }
    assert json.loads(call.args[1])["event_type"] == "refund.created"


@pytest.mark.parametrize(
    ("event_type", "topic"),
    [
        ("payment.initiated", PAYMENTS_TOPIC),
        ("payment.captured", PAYMENTS_TOPIC),
        ("refund.succeeded", PAYMENTS_TOPIC),
        ("dlq.v1", DLQ_TOPIC),
        ("ledger.transfer_posted", LEDGER_TOPIC),
        ("wallet.transfer", LEDGER_TOPIC),
    ],
)
def test_topic_routing(event_type: str, topic: str) -> None:
    assert topic_for(event_type) == topic


def test_key_falls_back_from_payment_id_to_id_to_event_id() -> None:
    assert key_for(make_event(1, payload={"payment_id": "p"})) == b"p"
    assert key_for(make_event(2, "dlq.v1", {"id": "x"})) == b"x"
    event = make_event(3, payload={"other": 1})
    assert key_for(event) == str(event.id).encode()


def test_value_adds_event_id_and_type_without_dropping_the_payload() -> None:
    event = make_event(4, payload={"payment_id": "p", "amount": 5})
    assert json.loads(value_for(event)) == {
        "payment_id": "p",
        "amount": 5,
        "event_id": str(event.id),
        "event_type": "payment.initiated",
    }
