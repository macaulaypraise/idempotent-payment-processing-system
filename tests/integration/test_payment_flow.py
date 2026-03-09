"""
Integration tests for the full payment flow.
Requires: real PostgreSQL + Redis (CI sidecars or docker-compose.test.yml)

Covers: state machine, outbox creation, outbox polling, payment retrieval.
Note: test_invalid_state_transition_raises is a pure unit test — it lives
here for discoverability but requires no real infrastructure.
"""

from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidStateTransitionError
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.services.outbox_poller import poll_and_publish
from app.services.payment_service import create_payment
from app.services.state_machine import validate_transition

# Test 1: State machine — pure logic, no I/O needed


@pytest.mark.asyncio
async def test_invalid_state_transition_raises(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """PENDING → REFUNDED must raise InvalidStateTransitionError."""
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.REFUNDED)


# Test 2: Outbox row created alongside payment, published_at is NULL


@pytest.mark.asyncio
async def test_outbox_row_created_with_null_published_at(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Payment creation must produce an outbox row with published_at = NULL."""
    await create_payment(db_session, redis_client, "key-outbox-null-001", 75.00, "USD")

    result = await db_session.execute(
        select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
    )
    unpublished = result.scalars().all()
    assert len(unpublished) >= 1


# Test 3: Outbox published_at set after poll


@pytest.mark.asyncio
async def test_outbox_published_after_poll(
    db_session: AsyncSession, redis_client: Redis, mock_kafka_producer: Any
) -> None:
    """Create payment, run poll_and_publish once → outbox row has published_at set."""
    await create_payment(db_session, redis_client, "key-outbox-001", 75.00, "USD")

    result = await db_session.execute(
        select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
    )
    unpublished = result.scalars().all()
    assert len(unpublished) == 1

    processed_count, _ = await poll_and_publish(db_session, mock_kafka_producer)
    assert processed_count == 1

    await db_session.refresh(unpublished[0])
    assert unpublished[0].published_at is not None


# Test 4: Poll returns 0 when outbox is empty


@pytest.mark.asyncio
async def test_poll_returns_zero_on_empty_outbox(
    db_session: AsyncSession, mock_kafka_producer: Any
) -> None:
    """poll_and_publish on an empty outbox must return (0, 0.0)."""
    count, lag = await poll_and_publish(db_session, mock_kafka_producer)
    assert count == 0
    assert lag == 0.0
    mock_kafka_producer.send_and_wait.assert_not_awaited()


# Test 5: Payment row persists in DB with correct fields


@pytest.mark.asyncio
async def test_payment_row_persisted_correctly(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Payment row must have correct amount, currency, and status after creation."""
    idem_key = "key-persist-001"
    response = await create_payment(db_session, redis_client, idem_key, 99.99, "USD")

    result = await db_session.execute(
        select(Payment).where(Payment.idempotency_key == idem_key)
    )
    payment = result.scalar_one()

    assert str(payment.id) == response["payment_id"]
    assert float(payment.amount) == 99.99
    assert payment.currency == "USD"
    assert payment.status == PaymentStatus.PENDING


# Test 6: Kafka producer called with correct topic


@pytest.mark.asyncio
async def test_poll_publishes_to_correct_kafka_topic(
    db_session: AsyncSession, redis_client: Redis, mock_kafka_producer: Any
) -> None:
    """Outbox poller must publish to the payment.initiated topic."""
    await create_payment(db_session, redis_client, "key-topic-001", 50.00, "USD")
    await poll_and_publish(db_session, mock_kafka_producer)

    mock_kafka_producer.send_and_wait.assert_awaited_once()
    call_args = mock_kafka_producer.send_and_wait.call_args
    topic = call_args[0][0] if call_args[0] else call_args[1].get("topic")
    assert "payment" in topic
