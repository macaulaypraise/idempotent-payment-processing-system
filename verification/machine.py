"""The model-based test: a Hypothesis state machine over the whole system.

Each example is one adversarial history, run in-process against the real
database:
  - the API through httpx's ASGI transport (real routes, real idempotency),
  - the PSP simulator the same way, with its fault profile changed by rules,
  - the workers through their single steps (charger.handle, sweeper.run_once,
    webhook processor run_once, reconciler.run), driven by rules in any order,
  - Kafka replaced by a queue fed by the real outbox publisher: events can be
    consumed in any order and redelivered.

After every rule the safety invariants (I1 I2 I3 I5 I6) and replay identity
(I9) must hold. A quiesce rule, and the end of every example, run every
worker until nothing changes, reconcile, and then check completeness (I4,
I7) and the model: every row and wallet balance must be what the client's
acknowledged history and the provider's ground truth imply.

Kafka, real time and network faults are deliberately not in this loop; the
fault layer (Stage 7 part 2) covers them with the same checker.

Profiles (VERIFY_PROFILE): "pr" is short and derandomized for pull requests;
"nightly" is long and random, and prints the seed of any failure.
"""

import asyncio
import dataclasses
import logging
import os
import random
import uuid
from collections import Counter
from collections.abc import Coroutine
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

import structlog
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    multiple,
    precondition,
    rule,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from verification import checker, database
from verification.history import History

T = TypeVar("T")

settings.register_profile(
    "pr",
    max_examples=int(os.environ.get("VERIFY_EXAMPLES", "20")),
    stateful_step_count=30,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "nightly",
    max_examples=int(os.environ.get("VERIFY_EXAMPLES", "300")),
    stateful_step_count=50,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
# The self-test only needs to find the planted bug, not shrink it
settings.register_profile(
    "selftest",
    max_examples=int(os.environ.get("VERIFY_SELFTEST_EXAMPLES", "150")),
    stateful_step_count=25,
    derandomize=True,
    deadline=None,
    database=None,
    phases=[Phase.explicit, Phase.generate],
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

WEBHOOK_SECRET = "whsec_harness"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"  # refused at once: Redis is down
# The provider can misbehave in these ways; a rule switches between them
FAULT_PRESETS: dict[str, dict[str, Any]] = {
    "none": {},
    "lost_responses": {
        "charge_then_timeout_rate": 0.5,
        "refund_then_timeout_rate": 0.5,
    },
    "declines": {"decline_rate": 0.5, "refund_fail_rate": 0.5},
    "unavailable": {"unavailable_rate": 0.7},
    "slow": {"slow_rate": 0.5, "slow_seconds": 0.2},
    "webhook_chaos": {
        "webhook_drop_rate": 0.3,
        "webhook_duplicate_rate": 0.5,
        "webhook_delay_max_seconds": 30.0,
        "charge_then_timeout_rate": 0.3,
    },
}


class Harness:
    """One event loop and one engine per test run, shared by its examples."""

    def __init__(self, owner_url: str, app_url: str, redis_url: str | None) -> None:
        self.owner_url = owner_url
        self.app_url = app_url
        self.redis_url = redis_url
        self.loop = asyncio.new_event_loop()
        engine = create_async_engine(app_url, pool_size=10, max_overflow=10)
        self.engine = engine
        self.factory = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        self.examples = 0
        self.steps = 0
        # What the histories actually did, so a quiet harness cannot pass for
        # a thorough one: acknowledged writes, provider charges, end states
        self.stats: Counter[str] = Counter()
        self.history_dir = os.environ.get("VERIFY_HISTORY_DIR")

    def run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(coroutine)

    def close(self) -> None:
        self.run(self.engine.dispose())
        self.loop.close()


HARNESS: Harness | None = None


def configure(owner_url: str, app_url: str, redis_url: str | None = None) -> Harness:
    global HARNESS
    # Thousands of steps log every charge and retry; keep only errors so the
    # harness's own summary is readable (restored by shutdown()).
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR)
    )
    HARNESS = Harness(owner_url, app_url, redis_url)
    return HARNESS


def shutdown() -> None:
    global HARNESS
    if HARNESS is not None:
        HARNESS.close()
        HARNESS = None
    structlog.reset_defaults()


@dataclasses.dataclass(frozen=True)
class Request:
    """A client write the machine can retry: the key and the exact body."""

    op: str
    path: str
    key: str
    body: dict[str, Any]
    answer: Any  # the first response body


class _Queue:
    """Stands in for Kafka between the outbox publisher and the charger."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send_and_wait(
        self, topic: str, value: bytes, *, key: bytes, headers: list[Any]
    ) -> None:
        from app.workers.charger import HANDLED_EVENTS, decode

        event = decode(topic, value, headers)
        if event.event_type in HANDLED_EVENTS:
            self.events.append(event)


class LedgerlineMachine(RuleBasedStateMachine):
    wallets = Bundle("wallets")
    payments = Bundle("payments")
    refunds = Bundle("refunds")

    def __init__(self) -> None:
        super().__init__()
        assert HARNESS is not None, "call verification.machine.configure() first"
        self.h = HARNESS
        self.h.examples += 1
        self.history = History()
        self.queue = _Queue()
        self.fault_preset = "none"
        self.wallet_count = 0
        self.steps_since_faults = 0
        self.h.run(self._setup())

    # --- wiring -------------------------------------------------------------------

    async def _setup(self) -> None:
        from app.cli import create_tenant
        from app.clients.psp import PspClient
        from app.config import get_settings
        from app.core.clock import FakeClock, get_clock
        from app.core.redis_client import build_redis_client
        from app.dependencies import get_db_dep, get_response_cache, get_settings_dep
        from app.main import app
        from app.services.reconciliation import Reconciler
        from app.services.response_cache import ResponseCache
        from app.workers.charger import Charger
        from app.workers.sweeper import Sweeper
        from app.workers.webhook_processor import WebhookProcessor
        from psp_sim.main import create_app

        await database.reset(self.h.owner_url)
        self.clock = FakeClock(datetime.now(UTC))
        self.settings = get_settings().model_copy(
            update={
                "PSP_WEBHOOK_SECRET": WEBHOOK_SECRET,
                "PSP_TIMEOUT_SECONDS": 0.05,
                "PSP_MAX_ATTEMPTS": 2,  # so exhaustion (dead-lettered) is reachable
                "WEBHOOK_MAX_ATTEMPTS": 3,
                "WEBHOOK_RETRY_MAX_SECONDS": 60.0,
                "RECON_SETTLEMENT_WINDOW_DAYS": 0,
            }
        )

        def cache_for(url: str) -> tuple[Any, ResponseCache]:
            client = build_redis_client(url, self.settings.REDIS_SOCKET_TIMEOUT_SECONDS)
            return client, ResponseCache(
                client,
                ttl_seconds=self.settings.IDEMPOTENCY_TTL_SECONDS,
                cooldown_seconds=self.settings.REDIS_BREAKER_COOLDOWN_SECONDS,
            )

        # Redis is only a cache: a history runs with it up or down (drawn by
        # an initialize rule), and correctness must not change. With it down,
        # every replay goes through the Postgres path.
        self.redis, self.cache_up = cache_for(self.h.redis_url or UNREACHABLE_REDIS)
        self.dead_redis, self.cache_down = cache_for(UNREACHABLE_REDIS)
        if self.h.redis_url:
            await self.redis.flushdb()
        self.cache = self.cache_up
        factory = self.h.factory

        async def _db() -> Any:
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db_dep] = _db
        app.dependency_overrides[get_response_cache] = lambda: self.cache
        app.dependency_overrides[get_settings_dep] = lambda: self.settings
        app.dependency_overrides[get_clock] = lambda: self.clock
        self.api = AsyncClient(transport=ASGITransport(app=app), base_url="http://api")

        async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
            return (await self.api.post(url, content=body, headers=headers)).status_code

        self.sim_app = create_app(
            db_path=":memory:",
            response_timeout_seconds=0.2,
            webhook_url="/v1/webhooks/psp",
            webhook_secret=WEBHOOK_SECRET,
            webhook_sender=sender,
        )
        self.dispatcher = self.sim_app.state.webhooks
        self.dispatcher.clock = lambda: self.clock.now().timestamp()
        self.sim = AsyncClient(
            transport=ASGITransport(app=self.sim_app), base_url="http://sim"
        )
        psp = PspClient(self.sim, timeout_seconds=self.settings.PSP_TIMEOUT_SECONDS)
        rng = random.Random(self.h.examples)
        self.charger = Charger(factory, psp, self.clock, self.settings, rng)
        self.sweeper = Sweeper(factory, self.charger, self.clock, self.settings)
        self.processor = WebhookProcessor(factory, self.clock, self.settings, rng)
        self.reconciler = Reconciler(factory, psp, self.clock, self.settings)

        async with factory() as db:
            _, api_key = await create_tenant(db, f"harness-{self.h.examples}")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        # Initialize rules run in any order, so the first wallet exists first
        self.first_wallet_id = await self._wallet()

    def teardown(self) -> None:
        try:
            self.h.run(self._quiesce_and_check(final=True))
        finally:
            if self.h.history_dir:
                directory = Path(self.h.history_dir)
                self.history.dump(directory / "last.jsonl")
                import json

                (directory / "last-ground-truth.json").write_text(
                    json.dumps(self.ground_truth(), default=str)
                )
            self.h.run(self._close())

    async def _close(self) -> None:
        from app.main import app

        app.dependency_overrides.clear()
        await self.api.aclose()
        await self.sim.aclose()
        await self.redis.aclose()
        await self.dead_redis.aclose()

    def ground_truth(self) -> dict[str, Any]:
        return dict(self.sim_app.state.store.ground_truth())

    async def _call(self, op: str, path: str, key: str, body: dict[str, Any]) -> Any:
        try:
            response = await self.api.post(
                path, json=body, headers={**self.headers, "Idempotency-Key": key}
            )
        except Exception as exc:  # noqa: BLE001 - an unknown outcome, recorded as such
            self.history.record("client", op, key=key, request=body, body=repr(exc))
            return None
        answer = response.json()
        self.h.stats[f"{op} {response.status_code}"] += 1
        self.history.record(
            "client",
            op,
            key=key,
            request=body,
            status=response.status_code,
            body=answer,
        )
        return answer if response.status_code < 400 else None

    # --- client rules -------------------------------------------------------------

    @initialize(target=wallets)
    def first_wallet(self) -> uuid.UUID:
        """The wallet made during setup, put in the bundle for other rules."""
        return self.first_wallet_id

    @initialize(redis_up=st.booleans())
    def redis_state(self, redis_up: bool) -> None:
        self.cache = self.cache_up if redis_up else self.cache_down
        self.h.stats[f"redis {'up' if redis_up else 'down'}"] += 1
        self.history.record("fault", "redis", request={"up": redis_up})

    @initialize(preset=st.sampled_from(sorted(FAULT_PRESETS)))
    def first_faults(self, preset: str) -> None:
        """Every history starts with the provider already misbehaving."""
        self.set_faults(preset)

    @initialize(
        target=payments,
        amounts=st.lists(st.integers(100, 5_000), min_size=1, max_size=3),
    )
    def first_payments(self, amounts: list[int]) -> Any:
        """And with money in flight, so worker rules have work from step one."""
        requests = []
        for amount in amounts:
            request = self._pay(self.first_wallet_id, amount)
            if request is not None:
                requests.append(request)
        return multiple(*requests)

    async def _wallet(self) -> uuid.UUID:
        answer = await self._call(
            "create_wallet", "/v1/wallets", str(uuid.uuid4()), {"currency": "USD"}
        )
        assert answer is not None, "creating a wallet failed"
        self.wallet_count += 1
        return uuid.UUID(answer["wallet_id"])

    # Three wallets are enough for transfers; more only dilutes the history
    @precondition(lambda self: self.wallet_count < 3)
    @rule(target=wallets)
    def create_wallet(self) -> uuid.UUID:
        return self.h.run(self._wallet())

    @rule(target=payments, wallet=wallets, amount=st.integers(100, 5_000))
    def create_payment(self, wallet: uuid.UUID, amount: int) -> Any:
        request = self._pay(wallet, amount)
        return multiple() if request is None else request

    def _pay(self, wallet: uuid.UUID, amount: int) -> Request | None:
        key = str(uuid.uuid4())
        body = {"amount": amount, "currency": "USD", "wallet_id": str(wallet)}
        answer = self.h.run(self._call("create_payment", "/v1/payments", key, body))
        if answer is None:
            return None
        return Request("create_payment", "/v1/payments", key, body, answer)

    @rule(request=payments)
    def retry_payment(self, request: Request) -> None:
        """Same key, same body: must replay the first answer (I9)."""
        self.h.run(self._call(request.op, request.path, request.key, request.body))

    @rule(request=payments)
    def retry_payment_with_another_body(self, request: Request) -> None:
        """Same key, different body: must be refused with 422 (I9)."""
        body = {**request.body, "amount": request.body["amount"] + 1}
        self.h.run(self._call(request.op, request.path, request.key, body))

    @rule(target=refunds, payment=payments)
    def request_refund(self, payment: Request) -> Any:
        key = str(uuid.uuid4())
        body = {"payment_id": payment.answer["payment_id"]}
        answer = self.h.run(self._call("create_refund", "/v1/refunds", key, body))
        if answer is None:
            return multiple()  # not capturable yet, or already refunded
        return Request("create_refund", "/v1/refunds", key, body, answer)

    @rule(request=refunds)
    def retry_refund(self, request: Request) -> None:
        self.h.run(self._call(request.op, request.path, request.key, request.body))

    @rule(source=wallets, target_wallet=wallets, amount=st.integers(1, 3_000))
    def transfer(
        self, source: uuid.UUID, target_wallet: uuid.UUID, amount: int
    ) -> None:
        if source == target_wallet:
            return
        body = {
            "from_wallet_id": str(source),
            "to_wallet_id": str(target_wallet),
            "amount": amount,
        }
        self.h.run(
            self._call("create_transfer", "/v1/transfers", str(uuid.uuid4()), body)
        )

    # --- worker rules -------------------------------------------------------------

    @rule()
    def outbox_step(self) -> None:
        from app.services.outbox_poller import poll_and_publish

        async def step() -> None:
            async with self.h.factory() as db:
                await poll_and_publish(db, self.queue, batch_size=20)

        self.h.run(step())

    @precondition(lambda self: bool(self.queue.events))
    @rule(pick=st.integers(0, 50), redeliver=st.booleans())
    def charger_step(self, pick: int, redeliver: bool) -> None:
        """Consume any queued event (reordering), sometimes leaving it queued
        to be delivered again (at-least-once)."""
        index = pick % len(self.queue.events)
        event = self.queue.events[index] if redeliver else self.queue.events.pop(index)
        self.h.run(self.charger.handle(event))

    @rule(seconds=st.sampled_from([5, 90, 400]))
    def sweeper_step(self, seconds: int) -> None:
        self.clock.advance(seconds=seconds)
        self.h.run(self.sweeper.run_once())

    @precondition(lambda self: bool(self.dispatcher.store.webhooks("pending")))
    @rule(pick=st.integers(0, 50), duplicate=st.booleans())
    def deliver_webhook(self, pick: int, duplicate: bool) -> None:
        """Deliver any pending webhook (reordering), sometimes twice."""
        pending = self.dispatcher.store.webhooks("pending")
        webhook = pending[pick % len(pending)]
        if duplicate:
            webhook = dataclasses.replace(webhook, duplicate=1)
        self.h.run(self.dispatcher.deliver(webhook))

    @rule()
    def webhook_processor_step(self) -> None:
        self.h.run(self.processor.run_once())

    @rule()
    def reconcile(self) -> None:
        for day in self._dates():
            self.h.run(self.reconciler.run(day))

    # --- fault rules --------------------------------------------------------------

    # A new profile at most every few steps, so a profile has time to bite
    @precondition(lambda self: self.steps_since_faults >= 5)
    @rule(preset=st.sampled_from(sorted(FAULT_PRESETS)))
    def change_faults(self, preset: str) -> None:
        self.set_faults(preset)

    def set_faults(self, preset: str) -> None:
        self.steps_since_faults = 0
        self.fault_preset = preset
        self.h.stats[f"faults {preset}"] += 1
        self.h.run(self._faults(FAULT_PRESETS[preset]))

    async def _faults(self, profile: dict[str, Any]) -> None:
        response = await self.sim.put("/admin/faults", json=profile)
        assert response.status_code == 200, response.text
        self.history.record("fault", "set_faults", request=profile, status=200)

    # --- quiesce ------------------------------------------------------------------

    @rule(retries_first=st.booleans())
    def quiesce(self, retries_first: bool) -> None:
        self.h.run(self._quiesce_and_check(retries_first=retries_first))

    def _dates(self) -> list[date]:
        truth = self.ground_truth()
        days = {
            date.fromisoformat(row["created_at"][:10])
            for row in truth["charges"] + truth["refunds"]
        }
        return sorted(days | {datetime.now(UTC).date()})

    async def _drain_once(self, retries_first: bool) -> int:
        """One pass of every worker; returns how much work it did.

        `retries_first` lets the sweeper's retries race ahead of the
        provider's webhooks; otherwise webhooks land first. Both orders
        happen in production, and each can hide a bug the other exposes (a
        webhook that heals a lost response masks a retry that would charge
        twice)."""
        from app.services.outbox_poller import poll_and_publish

        async def retries() -> int:
            self.clock.advance(seconds=400)  # retries, leases, parked webhooks due
            return len(await self.sweeper.run_once())

        async def webhooks() -> int:
            delivered: int = await self.dispatcher.drain()
            return delivered + len(await self.processor.run_once())

        work = 0
        async with self.h.factory() as db:
            work += (await poll_and_publish(db, self.queue, batch_size=100)).published
        while self.queue.events:
            await self.charger.handle(self.queue.events.pop(0))
            work += 1
        for step in (retries, webhooks) if retries_first else (webhooks, retries):
            work += await step()
        return work

    async def _quiesce_and_check(
        self, *, retries_first: bool = True, final: bool = False
    ) -> None:
        # Work already in flight meets the provider as it is right now, the
        # way a live system would; only then does the provider recover.
        # (Clearing the faults first let pending payments skip every fault,
        # which hid the harness's own planted bug.)
        await self._drain_once(retries_first)
        await self._faults({})
        for _ in range(25):
            if await self._drain_once(retries_first) == 0:
                break
        else:  # pragma: no cover - would mean a retry loop that never ends
            raise AssertionError("system did not quiesce in 25 rounds")
        for day in self._dates():
            await self.reconciler.run(day)
        truth = self.ground_truth()
        async with self.h.factory() as db:
            violations = await checker.check_all(db, self.history, truth)
            if final:
                self.h.stats["provider charges"] += len(truth["charges"])
                self.h.stats["provider refunds"] += len(truth["refunds"])
                for status, count in await _statuses(db):
                    self.h.stats[f"payments ending {status}"] += count
        assert not violations, "after quiesce:\n" + "\n".join(violations)

    # --- invariants after every step ----------------------------------------------

    @invariant()
    def safety(self) -> None:
        self.h.steps += 1
        self.steps_since_faults += 1

        async def check() -> list[str]:
            async with self.h.factory() as db:
                return await checker.safety(db, self.ground_truth())

        violations = self.h.run(check()) + checker.replays(self.history)
        assert not violations, "\n".join(violations)


async def _statuses(db: AsyncSession) -> list[tuple[str, int]]:
    from sqlalchemy import text

    rows = await db.execute(text("SELECT status, count(*) FROM payments GROUP BY 1"))
    return [(str(r[0]), int(r[1])) for r in rows.all()]
