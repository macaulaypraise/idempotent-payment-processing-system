"""Deletes idempotency key rows past expires_at (default 24 h).

`run_once` does the work and is what tests and the Stage 7 harness call;
the loop below only repeats it.
"""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.clock import Clock, SystemClock
from app.core.database import AsyncSessionFactory
from app.core.logging import setup_logging
from app.core.metrics import idempotency_keys_expired_total
from app.core.shutdown import sleep_or_stop, stop_on_signals
from app.services.idempotency import delete_expired_keys

logger = structlog.get_logger()


async def run_once(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, batch_size: int
) -> int:
    """Delete every currently expired key, in batches. Returns rows deleted."""
    total = 0
    now = clock.now()
    while True:
        async with session_factory() as db:
            deleted = await delete_expired_keys(db, now=now, batch_size=batch_size)
        total += deleted
        if deleted < batch_size:
            break
    if total:
        idempotency_keys_expired_total.inc(total)
        logger.info("idempotency_keys_expired", count=total)
    return total


async def main() -> None:  # pragma: no cover - loop around run_once
    setup_logging()
    settings = get_settings()
    clock = SystemClock()
    stop = stop_on_signals("idempotency_expiry")
    logger.info("idempotency_expiry_started")
    while not stop.is_set():
        try:
            await run_once(
                AsyncSessionFactory, clock, settings.IDEMPOTENCY_EXPIRY_BATCH_SIZE
            )
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.error("idempotency_expiry_failed", error=str(exc))
        await sleep_or_stop(settings.IDEMPOTENCY_EXPIRY_INTERVAL_SECONDS, stop)
    logger.info("idempotency_expiry_stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
