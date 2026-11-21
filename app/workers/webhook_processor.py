"""Applies stored provider webhooks (DESIGN 6.3).

Each event is handled in one transaction: claim the inbox row with FOR UPDATE
SKIP LOCKED, apply the outcome through apply_psp_result / apply_refund_result
(the same entry point the charger uses), and record the row's new status. The
effect and "processed" commit together or not at all, so a crash leaves the
row claimable and the next attempt finds the effect either absent or already
applied.

Outcomes:
  charge.succeeded / charge.failed  -> payment (merchant_reference = payment id)
  refund.succeeded / refund.failed  -> refund (merchant_reference = refund id)
  applied or already applied        -> processed
  payment/refund unknown, or not in a state the outcome applies to yet
                                    -> parked, retried with backoff;
                                       dead + dlq.v1 after WEBHOOK_MAX_ATTEMPTS
  contradicts what is recorded      -> dead + dlq.v1 at once (never auto-applied)
  malformed, or a pre-ledger payment -> dead + dlq.v1
  any other event type              -> ignored

Several processors can run: SKIP LOCKED keeps them off each other's rows.
Lock order is the inbox row, then the payment, refund and accounts, and
nothing else locks inbox rows, so the charger cannot deadlock against it.
"""

import asyncio
import enum
import random
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.backoff import equal_jitter
from app.core.clock import Clock, SystemClock
from app.core.failpoints import failpoint
from app.core.metrics import dlq_depth, webhooks_processed_total
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment
from app.models.refund import Refund
from app.models.tenant import DEFAULT_TENANT_ID
from app.models.webhook_event import WebhookEvent, WebhookStatus
from app.services.psp_results import (
    Applied,
    ConflictingOutcomeError,
    LegacyPaymentError,
    NotApplicableYetError,
    OutcomeKind,
    PaymentNotFoundError,
    PspOutcome,
    RefundOutcome,
    RefundOutcomeKind,
    apply_psp_result,
    apply_refund_result,
)

logger = structlog.get_logger()

SOURCE = "webhook"
DLQ_EVENT = "dlq.v1"
CHARGE_EVENTS = {"charge.succeeded", "charge.failed"}
REFUND_EVENTS = {"refund.succeeded", "refund.failed"}


class Outcome(enum.StrEnum):
    PROCESSED = "processed"
    ALREADY_APPLIED = "already_applied"
    PARKED = "parked"
    DEAD = "dead"
    IGNORED = "ignored"


class MalformedEventError(Exception):
    pass


@dataclass
class WebhookProcessor:
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    settings: Settings
    rng: random.Random = field(default_factory=random.Random)
    concurrency: int = 4

    def backoff_seconds(self, attempts: int) -> float:
        return equal_jitter(
            attempts,
            self.settings.WEBHOOK_RETRY_BASE_SECONDS,
            self.settings.WEBHOOK_RETRY_MAX_SECONDS,
            self.rng,
        )

    async def run_once(self) -> list[Outcome]:
        """Process due events until none are left or a batch is done."""
        budget = self.settings.WEBHOOK_BATCH_SIZE
        outcomes: list[Outcome] = []

        async def lane() -> None:
            nonlocal budget
            while budget > 0:
                budget -= 1
                outcome = await self.process_one()
                if outcome is None:
                    return
                outcomes.append(outcome)

        await asyncio.gather(*[lane() for _ in range(self.concurrency)])
        return outcomes

    async def process_one(self) -> Outcome | None:
        """Claim and handle one due event; None when nothing is due."""
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(WebhookEvent)
                    .where(
                        WebhookEvent.status.in_(
                            [WebhookStatus.RECEIVED, WebhookStatus.PARKED]
                        ),
                        WebhookEvent.next_attempt_at <= self.clock.now(),
                    )
                    .order_by(WebhookEvent.next_attempt_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.attempts += 1
            outcome = await self._handle(db, row)
            # The effect and the inbox status are both still uncommitted: a
            # crash here loses neither, the row is simply claimed again.
            failpoint("webhook_processor.before_commit")
            await db.commit()
        webhooks_processed_total.labels(
            event_type=row.event_type, result=outcome.value
        ).inc()
        if outcome is Outcome.DEAD:
            dlq_depth.inc()
        return outcome

    async def _handle(self, db: AsyncSession, row: WebhookEvent) -> Outcome:
        if row.event_type not in CHARGE_EVENTS | REFUND_EVENTS:
            return self._finish(row, WebhookStatus.IGNORED, Outcome.IGNORED)
        try:
            target = await self._resolve(db, row)
            # The tenant is recorded before the savepoint: attributes changed
            # inside one are expired when it rolls back, and an expired
            # attribute cannot be lazy-loaded in async code.
            row.tenant_id = target.tenant_id
            async with db.begin_nested():
                applied = await self._apply(db, row, target)
        except (PaymentNotFoundError, NotApplicableYetError) as exc:
            return self._park(db, row, str(exc))
        except ConflictingOutcomeError as exc:
            return self._dead(db, row, f"conflicting outcome: {exc}")
        except (MalformedEventError, LegacyPaymentError) as exc:
            return self._dead(db, row, str(exc))
        outcome = (
            Outcome.PROCESSED if applied is Applied.APPLIED else Outcome.ALREADY_APPLIED
        )
        return self._finish(row, WebhookStatus.PROCESSED, outcome)

    def _park(self, db: AsyncSession, row: WebhookEvent, error: str) -> Outcome:
        if row.attempts >= self.settings.WEBHOOK_MAX_ATTEMPTS:
            return self._dead(db, row, f"gave up: {error}")
        row.status = WebhookStatus.PARKED
        row.last_error = error
        row.next_attempt_at = self.clock.now() + timedelta(
            seconds=self.backoff_seconds(row.attempts)
        )
        logger.info(
            "webhook_parked",
            event_id=row.provider_event_id,
            attempts=row.attempts,
            error=error,
        )
        return Outcome.PARKED

    def _finish(
        self, row: WebhookEvent, status: WebhookStatus, outcome: Outcome
    ) -> Outcome:
        row.status = status
        row.processed_at = self.clock.now()
        row.next_attempt_at = None
        row.last_error = None
        return outcome

    def _dead(self, db: AsyncSession, row: WebhookEvent, error: str) -> Outcome:
        row.status = WebhookStatus.DEAD
        row.last_error = error
        row.next_attempt_at = None
        row.processed_at = self.clock.now()
        tenant_id = row.tenant_id or DEFAULT_TENANT_ID
        db.add(
            OutboxEvent(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                event_type=DLQ_EVENT,
                payload={
                    "kind": "webhook",
                    "id": str(row.id),
                    "tenant_id": str(tenant_id),
                    "provider_event_id": row.provider_event_id,
                    "event_type": row.event_type,
                    "attempts": row.attempts,
                    "error": error,
                },
            )
        )
        logger.error(
            "webhook_dead_lettered", event_id=row.provider_event_id, error=error
        )
        return Outcome.DEAD

    async def _resolve(self, db: AsyncSession, row: WebhookEvent) -> "Target":
        data = _data(row.payload)
        reference = _uuid(data.get("merchant_reference"), "merchant_reference")
        model: type[Payment] | type[Refund] = (
            Payment if row.event_type in CHARGE_EVENTS else Refund
        )
        tenant_id = (
            await db.execute(select(model.tenant_id).where(model.id == reference))
        ).scalar_one_or_none()
        if tenant_id is None:
            # Possibly not committed yet (the provider can be faster than us),
            # so park rather than drop.
            raise PaymentNotFoundError(f"{model.__tablename__} {reference} not found")
        return Target(reference, uuid.UUID(str(tenant_id)), data)

    @staticmethod
    async def _apply(db: AsyncSession, row: WebhookEvent, target: "Target") -> Applied:
        psp_id = target.data.get("id")
        if row.event_type in CHARGE_EVENTS:
            outcome = (
                PspOutcome(OutcomeKind.CAPTURED, psp_id)
                if row.event_type == "charge.succeeded"
                else PspOutcome(
                    OutcomeKind.DECLINED, psp_id, target.data.get("decline_reason")
                )
            )
            return await apply_psp_result(
                db,
                tenant_id=target.tenant_id,
                payment_id=target.id,
                outcome=outcome,
                source=SOURCE,
            )
        refund_outcome = RefundOutcome(
            RefundOutcomeKind.SUCCEEDED
            if row.event_type == "refund.succeeded"
            else RefundOutcomeKind.FAILED,
            psp_id,
            target.data.get("failure_reason"),
        )
        return await apply_refund_result(
            db,
            tenant_id=target.tenant_id,
            refund_id=target.id,
            outcome=refund_outcome,
            source=SOURCE,
        )


@dataclass(frozen=True)
class Target:
    id: uuid.UUID  # payment id or refund id
    tenant_id: uuid.UUID
    data: dict[str, Any]


def _data(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MalformedEventError("event has no data object")
    return data


def _uuid(value: Any, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise MalformedEventError(f"{name} {value!r} is not one of our ids") from exc


async def main() -> None:  # pragma: no cover - loop around run_once()
    from app.core.database import AsyncSessionFactory
    from app.core.logging import setup_logging

    setup_logging()
    settings = get_settings()
    processor = WebhookProcessor(AsyncSessionFactory, SystemClock(), settings)
    logger.info("webhook_processor_started")
    while True:
        try:
            outcomes = await processor.run_once()
        except Exception as exc:  # noqa: BLE001 - keep processing
            logger.error("webhook_processor_failed", error=str(exc))
            outcomes = []
        if len(outcomes) < settings.WEBHOOK_BATCH_SIZE:
            await asyncio.sleep(settings.WEBHOOK_POLL_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
