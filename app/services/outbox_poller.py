"""Publishes outbox rows to Kafka in insert order (ADR 0005).

One batch: the oldest unpublished rows by seq, sent one at a time and
acknowledged by the broker (idempotent producer, acks=all) before the next.
On the first failure the batch stops: later rows are not skipped past, so
one payment's events are never published out of order. What was sent is
marked published and committed; the rest is retried on the next round.

Delivery is at least once. If the relay crashes after Kafka acknowledged a
row but before the commit, that row is sent again; consumers deduplicate on
the event_id header (the outbox id).

Topics: every payment.* and refund.* event goes to payments.v1, keyed by
payment id, so a payment's events share a partition and stay in order;
dlq.v1 has its own topic; ledger.v1 is reserved for ledger events.
"""

import json
from datetime import UTC, datetime
from typing import Any, NamedTuple

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import outbox_lag_seconds, outbox_publish_lag_seconds
from app.models.outbox_event import OutboxEvent

logger = structlog.get_logger()

PAYMENTS_TOPIC = "payments.v1"
DLQ_TOPIC = "dlq.v1"
LEDGER_TOPIC = "ledger.v1"


class PublishResult(NamedTuple):
    published: int
    lag: float  # age in seconds of the oldest unpublished row seen
    failed: bool  # a send failed; the batch stopped there


def topic_for(event_type: str) -> str:
    if event_type == "dlq.v1":
        return DLQ_TOPIC
    if event_type.startswith(("ledger.", "wallet.", "transfer.")):
        return LEDGER_TOPIC
    return PAYMENTS_TOPIC


def key_for(event: Any) -> bytes:
    payload = event.payload if isinstance(event.payload, dict) else {}
    key = payload.get("payment_id") or payload.get("id") or str(event.id)
    return str(key).encode()


def value_for(event: Any) -> bytes:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return json.dumps(
        {**payload, "event_id": str(event.id), "event_type": event.event_type}
    ).encode("utf-8")


async def poll_and_publish(
    db: AsyncSession, kafka_producer: Any, batch_size: int = 100
) -> PublishResult:
    events = (
        (
            await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.seq)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if not events:
        outbox_lag_seconds.set(0)
        return PublishResult(0, 0.0, False)

    oldest = min(e.created_at for e in events)
    lag = (datetime.now(UTC) - oldest).total_seconds()
    outbox_lag_seconds.set(lag)

    count = 0
    failed = False
    for event in events:
        try:
            await kafka_producer.send_and_wait(
                topic_for(event.event_type),
                value_for(event),
                key=key_for(event),
                headers=[
                    ("event_id", str(event.id).encode()),
                    ("event_type", event.event_type.encode()),
                ],
            )
        except Exception as exc:  # noqa: BLE001 - stop here, retry next round
            logger.error(
                "event_publish_failed",
                event_id=str(event.id),
                seq=event.seq,
                error=str(exc),
            )
            failed = True
            break
        now = datetime.now(UTC)
        event.published_at = now
        outbox_publish_lag_seconds.observe((now - event.created_at).total_seconds())
        count += 1

    await db.commit()
    return PublishResult(count, lag, failed)
