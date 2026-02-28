import pytest
from app.services.payment_service import create_payment
from app.services.outbox_poller import poll_and_publish
from app.models.payment import Payment, PaymentStatus
from app.models.outbox_event import OutboxEvent
from app.core.exceptions import InvalidStateTransitionError
from app.services.state_machine import validate_transition
from sqlalchemy import select


async def test_invalid_state_transition_raises(db_session, redis_client):
    """PENDING → REFUNDED must raise InvalidStateTransitionError."""
    from app.models.payment import PaymentStatus
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.REFUNDED)


async def test_outbox_published_after_poll(db_session, redis_client, mock_kafka_producer):
    """Create payment, run poll_and_publish once → outbox row has published_at set."""
    await create_payment(db_session, redis_client, "key-outbox-001", 75.00, "USD")

    # verify outbox row exists with published_at = NULL
    result = await db_session.execute(
        select(OutboxEvent).where(OutboxEvent.published_at == None)
    )
    unpublished = result.scalars().all()
    assert len(unpublished) == 1

    # run the poller once
    count = await poll_and_publish(db_session, mock_kafka_producer)
    assert count == 1

    # verify published_at is now set
    await db_session.refresh(unpublished[0])
    assert unpublished[0].published_at is not None
