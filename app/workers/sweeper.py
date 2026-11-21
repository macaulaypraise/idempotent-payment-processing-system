"""The sweeper: re-drives payments and refunds the charger left unfinished.

It claims, in one statement per table with FOR UPDATE SKIP LOCKED:
  - PROCESSING rows whose next_attempt_at has passed (a scheduled retry, or a
    lease that expired because the charger crashed mid-attempt), and
  - PENDING rows older than SWEEPER_PENDING_AFTER_SECONDS (their event was
    lost),
pushing each lease forward and counting the attempt, then runs the
charger's call-and-apply path on each. Several sweepers can run at once;
SKIP LOCKED keeps them off each other's rows.

Rows without a wallet (pre-ledger payments) are never claimed.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import build_psp_client
from app.config import Settings, get_settings
from app.core.clock import Clock, SystemClock
from app.workers.charger import Charger, Claim, Result

logger = structlog.get_logger()

_CLAIM_PAYMENTS = text(
    """
    UPDATE payments
    SET status = 'PROCESSING', attempts = attempts + 1, next_attempt_at = :lease_until
    WHERE id IN (
        SELECT id FROM payments
        WHERE wallet_id IS NOT NULL
          AND ((status = 'PROCESSING' AND next_attempt_at IS NOT NULL
                AND next_attempt_at <= :now)
               OR (status = 'PENDING' AND created_at <= :pending_cutoff))
        ORDER BY coalesce(next_attempt_at, created_at)
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, tenant_id, attempts, amount, currency
    """
)

_CLAIM_REFUNDS = text(
    """
    UPDATE refunds r
    SET status = 'PROCESSING', attempts = r.attempts + 1,
        next_attempt_at = :lease_until, updated_at = :now
    FROM payments p
    WHERE p.id = r.payment_id
      AND r.id IN (
        SELECT id FROM refunds
        WHERE (status = 'PROCESSING' AND next_attempt_at IS NOT NULL
               AND next_attempt_at <= :now)
           OR (status = 'PENDING' AND created_at <= :pending_cutoff)
        ORDER BY coalesce(next_attempt_at, created_at)
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
      )
    RETURNING r.id, r.tenant_id, r.attempts, r.amount, r.currency, p.psp_reference
    """
)


@dataclass
class Sweeper:
    session_factory: async_sessionmaker[AsyncSession]
    charger: Charger
    clock: Clock
    settings: Settings
    concurrency: int = 10

    async def claim_due(self) -> list[Claim]:
        now = self.clock.now()
        params = {
            "now": now,
            "lease_until": self.charger.lease_until(),
            "pending_cutoff": now
            - timedelta(seconds=self.settings.SWEEPER_PENDING_AFTER_SECONDS),
            "limit": self.settings.SWEEPER_BATCH_SIZE,
        }
        async with self.session_factory() as db:
            payments = (await db.execute(_CLAIM_PAYMENTS, params)).all()
            refunds = (await db.execute(_CLAIM_REFUNDS, params)).all()
            await db.commit()
        return [
            Claim("payment", r.id, r.tenant_id, r.attempts, r.amount, r.currency)
            for r in payments
        ] + [
            Claim(
                "refund",
                r.id,
                r.tenant_id,
                r.attempts,
                r.amount,
                r.currency,
                r.psp_reference,
            )
            for r in refunds
        ]

    async def run_once(self) -> list[Result]:
        """Claim one batch and attempt each claim; returns the results."""
        claims = await self.claim_due()
        if not claims:
            return []
        gate = asyncio.Semaphore(self.concurrency)

        async def run(claim: Claim) -> Result:
            async with gate:
                if claim.kind == "refund" and claim.charge_id is None:
                    return await self.charger.dead_letter(
                        claim, "payment has no psp_reference"
                    )
                return await self.charger.attempt(claim)

        results = await asyncio.gather(*[run(c) for c in claims])
        logger.info(
            "sweeper_batch",
            claimed=len(claims),
            results={r.value: results.count(r) for r in set(results)},
        )
        return list(results)


async def main() -> None:  # pragma: no cover - loop around run_once()
    from app.core.database import AsyncSessionFactory
    from app.core.logging import setup_logging

    setup_logging()
    settings = get_settings()
    clock = SystemClock()
    psp = build_psp_client(settings.PSP_BASE_URL, settings.PSP_TIMEOUT_SECONDS)
    charger = Charger(AsyncSessionFactory, psp, clock, settings)
    sweeper = Sweeper(AsyncSessionFactory, charger, clock, settings)
    logger.info("sweeper_started")
    while True:
        try:
            results = await sweeper.run_once()
        except Exception as exc:  # noqa: BLE001 - keep sweeping
            logger.error("sweeper_failed", error=str(exc))
            results = []
        if len(results) < settings.SWEEPER_BATCH_SIZE:
            await asyncio.sleep(settings.SWEEPER_INTERVAL_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
