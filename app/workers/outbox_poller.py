"""Outbox relay: one leader publishes, woken by NOTIFY (ADR 0005).

Every replica opens a dedicated asyncpg connection and tries
pg_try_advisory_lock(OUTBOX_LOCK_KEY). The one that gets it is the leader:
it LISTENs on the outbox_events channel, which an AFTER INSERT trigger
notifies at commit, and publishes a batch whenever woken, or every
OUTBOX_FALLBACK_POLL_SECONDS in case a notification was missed. Standbys
retry the lock on the same interval, which bounds failover time.

The lock is tied to the connection: if the leader dies, Postgres releases
it when the session ends. Before every batch the leader checks pg_locks that
it still holds the lock; a dead connection fails that check and the replica
drops back to standby.

The one window this does not close: a leader whose lock connection has died
but whose current batch is still in flight can publish rows the new leader
also publishes. That is a duplicate, never a loss or a reorder within one
relay, and consumers deduplicate on event_id.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # type: ignore[import-untyped]  # no stubs; used for LISTEN only
import structlog
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.metrics import outbox_leader
from app.services.outbox_poller import PublishResult, poll_and_publish

logger = structlog.get_logger()

NOTIFY_CHANNEL = "outbox_events"
# Below 2**31 so pg_locks shows it as classid 0, objid OUTBOX_LOCK_KEY
OUTBOX_LOCK_KEY = 0x1D5_0B0C
_HOLDS_LOCK = """
    SELECT EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()
          AND classid = 0 AND objid = $1 AND objsubid = 1
    )
"""
FAILURE_BACKOFF_SECONDS = 1.0


class LeadershipLostError(Exception):
    pass


def asyncpg_dsn(database_url: str) -> str:
    """postgresql+asyncpg://... -> postgresql://... for asyncpg.connect."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


@dataclass
class OutboxRelay:
    dsn: str
    session_factory: async_sessionmaker[AsyncSession]
    producer: Any
    settings: Settings
    name: str = "relay"
    lock_key: int = OUTBOX_LOCK_KEY
    is_leader: bool = False
    published: int = 0
    backend_pid: int | None = None  # of the lock connection, for diagnostics
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                conn = await asyncpg.connect(self.dsn)
            except (OSError, asyncpg.PostgresError) as exc:
                logger.error(
                    "outbox_relay_connect_failed", relay=self.name, error=str(exc)
                )
                await self._sleep(self.settings.OUTBOX_FALLBACK_POLL_SECONDS, stop)
                continue
            try:
                await self._serve(conn, stop)
            except (
                LeadershipLostError,
                OSError,
                asyncpg.PostgresError,
                asyncpg.InterfaceError,
            ) as exc:
                logger.warning(
                    "outbox_relay_lost_leadership", relay=self.name, error=str(exc)
                )
            finally:
                self._step_down()
                with contextlib.suppress(Exception):
                    await asyncio.shield(conn.close(timeout=2))

    async def _serve(self, conn: asyncpg.Connection, stop: asyncio.Event) -> None:
        fallback = self.settings.OUTBOX_FALLBACK_POLL_SECONDS
        self.backend_pid = conn.get_server_pid()
        while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", self.lock_key):
            if await self._sleep(fallback, stop):
                return
        self.is_leader = True
        outbox_leader.set(1)
        logger.info("outbox_relay_leader", relay=self.name)
        await conn.add_listener(NOTIFY_CHANNEL, self._on_notify)
        while not stop.is_set():
            if not await conn.fetchval(_HOLDS_LOCK, self.lock_key):
                raise LeadershipLostError("advisory lock no longer held")
            # Cleared before publishing: a NOTIFY that lands mid-batch wakes
            # the next round instead of being lost.
            self._wake.clear()
            result = await self.publish_once()
            if result.failed:
                await self._sleep(FAILURE_BACKOFF_SECONDS, stop)
            elif result.published < self.settings.OUTBOX_BATCH_SIZE:
                await self._wait_for_work(fallback, stop)

    async def publish_once(self) -> PublishResult:
        async with self.session_factory() as db:
            result = await poll_and_publish(
                db, self.producer, self.settings.OUTBOX_BATCH_SIZE
            )
        self.published += result.published
        if result.published:
            logger.info(
                "outbox_events_published", relay=self.name, count=result.published
            )
        return result

    def _on_notify(self, *_: Any) -> None:
        self._wake.set()

    def _step_down(self) -> None:
        if self.is_leader:
            logger.info("outbox_relay_standby", relay=self.name)
        self.is_leader = False
        outbox_leader.set(0)

    async def _wait_for_work(self, timeout: float, stop: asyncio.Event) -> None:
        waiters = [
            asyncio.ensure_future(self._wake.wait()),
            asyncio.ensure_future(stop.wait()),
        ]
        try:
            await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()

    @staticmethod
    async def _sleep(seconds: float, stop: asyncio.Event) -> bool:
        """Sleep, waking early on stop; True if stopped."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        return stop.is_set()


async def main() -> None:  # pragma: no cover - wiring around OutboxRelay.run
    import os
    import socket

    from app.core.database import AsyncSessionFactory
    from app.core.kafka_client import close_kafka_producer, create_kafka_producer
    from app.core.logging import setup_logging
    from app.core.shutdown import stop_on_signals

    setup_logging()
    settings = get_settings()
    producer = await create_kafka_producer()
    relay = OutboxRelay(
        asyncpg_dsn(settings.DATABASE_URL),
        AsyncSessionFactory,
        producer,
        settings,
        name=f"{socket.gethostname()}:{os.getpid()}",
    )
    stop = stop_on_signals("outbox_relay")
    logger.info("outbox_relay_starting", relay=relay.name)
    try:
        await relay.run(stop)  # closing its connection releases the leader lock
    finally:
        await close_kafka_producer(producer)
        logger.info("outbox_relay_stopped", relay=relay.name)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
