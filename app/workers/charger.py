"""The charger: calls the provider for payments and refunds (DESIGN 6.2).

For every attempt:
  1. Short transaction: claim the row (PENDING -> PROCESSING), count the
     attempt, and set next_attempt_at as a lease; commit.
  2. No transaction open: call the provider with the payment (or refund) id as
     the idempotency key and an explicit timeout.
  3. Short transaction: apply the outcome through apply_psp_result /
     apply_refund_result and record the consumed event, together; commit.

A timeout or 5xx means "unknown": schedule a retry in Postgres with
exponential backoff and jitter, and ask again with the same key later. Past
PSP_MAX_ATTEMPTS, write a dlq.v1 event and stop; the row stays PROCESSING for
reconciliation, because the provider may well have charged.

Two workers calling the provider for the same payment is wasteful but safe
(same key, idempotent apply), so there is no lock to prevent it.

`handle(event)` and `attempt(claim)` are the units tests and the sweeper
drive; `main()` only feeds them from Kafka.
"""

import asyncio
import enum
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import (
    ChargeResult,
    PspClient,
    PspRejected,
    PspUnavailable,
    RefundResult,
    build_psp_client,
)
from app.config import Settings, get_settings
from app.core.clock import Clock, SystemClock
from app.core.failpoints import failpoint
from app.core.metrics import charger_attempts_total, dlq_depth
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.models.processed_event import ProcessedEvent
from app.models.refund import Refund, RefundStatus
from app.services.psp_results import (
    ConflictingOutcomeError,
    OutcomeKind,
    PspOutcome,
    RefundOutcome,
    RefundOutcomeKind,
    apply_psp_result,
    apply_refund_result,
    lock_payment,
    lock_refund,
    start_refund_processing,
)

logger = structlog.get_logger()

CONSUMER_GROUP = "charger"
TOPICS = ("payment.initiated", "refund.created")
DLQ_EVENT = "dlq.v1"


class Result(enum.StrEnum):
    CAPTURED = "captured"
    DECLINED = "declined"
    REFUNDED = "refunded"
    REFUND_FAILED = "refund_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    CONFLICT = "conflict"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class ChargerEvent:
    event_type: str
    payload: dict[str, Any]
    event_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Claim:
    kind: str  # "payment" | "refund"
    id: uuid.UUID
    tenant_id: uuid.UUID
    attempts: int
    amount: int
    currency: str = ""
    charge_id: str | None = None  # refunds: the provider charge to refund


@dataclass
class Charger:
    session_factory: async_sessionmaker[AsyncSession]
    psp: PspClient
    clock: Clock
    settings: Settings
    rng: random.Random = field(default_factory=random.Random)

    # --- scheduling -----------------------------------------------------------

    def lease_until(self) -> datetime:
        return self.clock.now() + timedelta(seconds=self.settings.PSP_LEASE_SECONDS)

    def backoff_seconds(self, attempts: int) -> float:
        """Exponential backoff with equal jitter: half fixed, half random."""
        ceiling = min(
            self.settings.PSP_RETRY_MAX_SECONDS,
            self.settings.PSP_RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0),
        )
        return float(ceiling / 2 + self.rng.uniform(0, ceiling / 2))

    # --- entry points ---------------------------------------------------------

    async def handle(self, event: ChargerEvent) -> Result:
        """Process one decoded payment.initiated or refund.created event."""
        if event.event_id and await self._already_processed(event.event_id):
            return Result.DUPLICATE
        tenant_id = uuid.UUID(event.payload["tenant_id"])
        if event.event_type == "payment.initiated":
            claim = await self._claim_payment(
                tenant_id, uuid.UUID(event.payload["payment_id"])
            )
        elif event.event_type == "refund.created":
            claim = await self._claim_refund(
                tenant_id, uuid.UUID(event.payload["refund_id"])
            )
        else:
            return Result.SKIPPED
        if claim is None:
            # Already claimed by an earlier delivery or the sweeper, finished,
            # or a pre-ledger payment: nothing for this event to do.
            await self._record_processed(event.event_id, tenant_id)
            return Result.SKIPPED
        return await self.attempt(claim, event_id=event.event_id)

    async def attempt(self, claim: Claim, event_id: uuid.UUID | None = None) -> Result:
        """Call the provider for a claimed row and apply what it says."""
        failpoint("charger.after_claim")
        result: ChargeResult | RefundResult
        try:
            if claim.kind == "payment":
                result = await self.psp.charge(
                    key=str(claim.id),
                    amount=claim.amount,
                    currency=claim.currency,
                    merchant_reference=str(claim.id),
                )
            else:
                assert claim.charge_id is not None
                result = await self.psp.refund(
                    key=str(claim.id), charge_id=claim.charge_id, amount=claim.amount
                )
        except PspUnavailable as exc:
            return await self._schedule_retry(claim, str(exc))
        except PspRejected as exc:
            return await self.dead_letter(claim, str(exc))
        # The provider has acted; nothing is recorded yet. A crash here is
        # what the provider idempotency key exists for.
        failpoint("charger.after_provider_call")
        return await self._apply(claim, result, event_id)

    # --- step 1: claim --------------------------------------------------------

    async def _claim_payment(
        self, tenant_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Claim | None:
        async with self.session_factory() as db:
            payment = await lock_payment(db, tenant_id=tenant_id, payment_id=payment_id)
            if payment is None or payment.wallet_id is None:
                return None
            if payment.status is not PaymentStatus.PENDING:
                return None
            payment.status = PaymentStatus.PROCESSING
            payment.attempts += 1
            payment.next_attempt_at = self.lease_until()
            claim = Claim(
                "payment",
                payment.id,
                tenant_id,
                payment.attempts,
                payment.amount,
                payment.currency,
            )
            await db.commit()
            return claim

    async def _claim_refund(
        self, tenant_id: uuid.UUID, refund_id: uuid.UUID
    ) -> Claim | None:
        async with self.session_factory() as db:
            payment, refund = await lock_refund(
                db, tenant_id=tenant_id, refund_id=refund_id
            )
            if refund.status is not RefundStatus.PENDING:
                return None
            await start_refund_processing(
                db,
                tenant_id=tenant_id,
                refund_id=refund_id,
                lease_until=self.lease_until(),
            )
            claim = Claim(
                "refund",
                refund.id,
                tenant_id,
                refund.attempts,
                refund.amount,
                refund.currency,
                payment.psp_reference,
            )
            await db.commit()
        if claim.charge_id is None:  # pragma: no cover - captured payments have one
            await self.dead_letter(claim, "payment has no psp_reference")
            return None
        return claim

    # --- step 3: apply --------------------------------------------------------

    async def _apply(
        self,
        claim: Claim,
        result: ChargeResult | RefundResult,
        event_id: uuid.UUID | None,
    ) -> Result:
        async with self.session_factory() as db:
            try:
                if isinstance(result, ChargeResult):
                    outcome = (
                        PspOutcome(OutcomeKind.CAPTURED, result.charge_id)
                        if result.succeeded
                        else PspOutcome(
                            OutcomeKind.DECLINED,
                            result.charge_id,
                            result.decline_reason,
                        )
                    )
                    await apply_psp_result(
                        db,
                        tenant_id=claim.tenant_id,
                        payment_id=claim.id,
                        outcome=outcome,
                        source="charger",
                    )
                    row: Payment | Refund | None = await db.get(Payment, claim.id)
                    done = Result.CAPTURED if result.succeeded else Result.DECLINED
                else:
                    refund_outcome = RefundOutcome(
                        RefundOutcomeKind.SUCCEEDED
                        if result.succeeded
                        else RefundOutcomeKind.FAILED,
                        result.refund_id,
                        result.failure_reason,
                    )
                    await apply_refund_result(
                        db,
                        tenant_id=claim.tenant_id,
                        refund_id=claim.id,
                        outcome=refund_outcome,
                        source="charger",
                    )
                    row = await db.get(Refund, claim.id)
                    done = Result.REFUNDED if result.succeeded else Result.REFUND_FAILED
            except ConflictingOutcomeError as exc:
                await db.rollback()
                logger.error(
                    "charger_conflicting_outcome",
                    kind=claim.kind,
                    id=str(claim.id),
                    error=str(exc),
                )
                charger_attempts_total.labels(
                    kind=claim.kind, result=Result.CONFLICT
                ).inc()
                await self.dead_letter(claim, f"conflicting outcome: {exc}")
                return Result.CONFLICT
            assert row is not None
            row.next_attempt_at = None
            await self._record_processed_in(db, event_id, claim.tenant_id)
            await db.commit()
        charger_attempts_total.labels(kind=claim.kind, result=done).inc()
        logger.info(
            "charger_outcome_applied",
            kind=claim.kind,
            id=str(claim.id),
            result=done.value,
            attempts=claim.attempts,
        )
        return done

    # --- unknown outcome: retry later, or dead-letter -------------------------

    async def _locked_row(
        self, db: AsyncSession, claim: Claim
    ) -> Payment | Refund | None:
        if claim.kind == "payment":
            return await lock_payment(
                db, tenant_id=claim.tenant_id, payment_id=claim.id
            )
        _, refund = await lock_refund(db, tenant_id=claim.tenant_id, refund_id=claim.id)
        return refund

    async def _schedule_retry(self, claim: Claim, error: str) -> Result:
        async with self.session_factory() as db:
            row = await self._locked_row(db, claim)
            if row is None or row.status.name != "PROCESSING":
                return Result.SKIPPED  # someone else finished it meanwhile
            if row.attempts >= self.settings.PSP_MAX_ATTEMPTS:
                await db.rollback()
                return await self.dead_letter(claim, error)
            delay = self.backoff_seconds(row.attempts)
            row.next_attempt_at = self.clock.now() + timedelta(seconds=delay)
            await db.commit()
        charger_attempts_total.labels(
            kind=claim.kind, result=Result.RETRY_SCHEDULED
        ).inc()
        logger.warning(
            "charger_retry_scheduled",
            kind=claim.kind,
            id=str(claim.id),
            attempts=row.attempts,
            delay_seconds=round(delay, 2),
            error=error,
        )
        return Result.RETRY_SCHEDULED

    async def dead_letter(self, claim: Claim, error: str) -> Result:
        async with self.session_factory() as db:
            row = await self._locked_row(db, claim)
            if row is None or row.status.name != "PROCESSING":
                return Result.SKIPPED
            row.next_attempt_at = None  # the sweeper leaves it; reconciliation decides
            db.add(
                OutboxEvent(
                    id=uuid.uuid4(),
                    tenant_id=claim.tenant_id,
                    event_type=DLQ_EVENT,
                    payload={
                        "kind": claim.kind,
                        "id": str(claim.id),
                        "tenant_id": str(claim.tenant_id),
                        "attempts": row.attempts,
                        "error": error,
                    },
                )
            )
            await db.commit()
        dlq_depth.inc()
        charger_attempts_total.labels(
            kind=claim.kind, result=Result.DEAD_LETTERED
        ).inc()
        logger.error(
            "charger_dead_lettered", kind=claim.kind, id=str(claim.id), error=error
        )
        return Result.DEAD_LETTERED

    # --- consumed-event bookkeeping -------------------------------------------

    async def _already_processed(self, event_id: uuid.UUID) -> bool:
        async with self.session_factory() as db:
            found = await db.execute(
                select(ProcessedEvent.event_id).where(
                    ProcessedEvent.event_id == event_id,
                    ProcessedEvent.consumer_group == CONSUMER_GROUP,
                )
            )
            return found.scalar_one_or_none() is not None

    async def _record_processed(
        self, event_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> None:
        if event_id is None:
            return
        async with self.session_factory() as db:
            await self._record_processed_in(db, event_id, tenant_id)
            await db.commit()

    @staticmethod
    async def _record_processed_in(
        db: AsyncSession, event_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> None:
        """Recorded with the effect, never before it: recording first would
        turn a crash into a lost payment."""
        if event_id is None:
            return
        await db.execute(
            insert(ProcessedEvent)
            .values(
                event_id=event_id, consumer_group=CONSUMER_GROUP, tenant_id=tenant_id
            )
            .on_conflict_do_nothing()
        )


def decode(
    topic: str, value: str, headers: list[tuple[str, bytes]] | None
) -> ChargerEvent:
    meta = {k: v.decode() for k, v in (headers or [])}
    event_id = uuid.UUID(meta["event_id"]) if "event_id" in meta else None
    return ChargerEvent(meta.get("event_type", topic), json.loads(value), event_id)


async def main() -> None:  # pragma: no cover - Kafka loop around handle()
    from app.core.database import AsyncSessionFactory
    from app.core.kafka_client import close_kafka_consumer, create_kafka_consumer
    from app.core.logging import setup_logging

    setup_logging()
    settings = get_settings()
    psp = build_psp_client(settings.PSP_BASE_URL, settings.PSP_TIMEOUT_SECONDS)
    charger = Charger(AsyncSessionFactory, psp, SystemClock(), settings)
    consumer = await create_kafka_consumer(TOPICS, CONSUMER_GROUP, auto_commit=False)
    logger.info("charger_started", topics=TOPICS)
    try:
        async for message in consumer:
            try:
                event = decode(
                    message.topic, message.value, list(message.headers or [])
                )
                await charger.handle(event)
            except Exception as exc:  # noqa: BLE001
                # The row is either unclaimed (the sweeper picks up PENDING
                # rows whose event was lost) or leased (the sweeper retries it),
                # so committing the offset cannot lose a payment.
                logger.error(
                    "charger_event_failed",
                    topic=message.topic,
                    offset=message.offset,
                    error=str(exc),
                )
            await consumer.commit()
    finally:
        await close_kafka_consumer(consumer)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
