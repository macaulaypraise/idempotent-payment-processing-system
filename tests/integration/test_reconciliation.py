"""
Stage 6 exit gate: the provider's settlement file and the books agree, and
every way they can disagree is detected and classified.

Real database, in-process PSP simulator (its settlement endpoint serves the
file), the charger to create real captures and refunds, and the reconciler
driven directly. Dates are taken from the simulator's own charges, so a test
that runs across midnight still reconciles the right day.
"""

import random
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.psp import PspClient
from app.config import get_settings
from app.core.clock import FakeClock
from app.core.failpoints import SimulatedCrash, armed
from app.models.ledger import AccountKind, Transfer
from app.models.payment import Payment, PaymentStatus
from app.models.reconciliation import Classification, ReconItem, ReconRun, RunStatus
from app.models.refund import Refund, RefundStatus
from app.models.wallet import Wallet
from app.services.ledger import balances
from app.services.payment_service import create_payment
from app.services.reconciliation import (
    Reconciler,
    ReconInProgressError,
    SettlementFileError,
)
from app.services.refunds import request_refund
from app.services.wallets import wallet_accounts
from app.workers.charger import Charger, ChargerEvent, Result
from psp_sim.main import create_app
from psp_sim.settlement import fee_for
from verification.invariants import check_ledger, i7_clearing_accounts_settled

Factory = async_sessionmaker[AsyncSession]


class Env:
    def __init__(self, factory: Factory, sim_app: Any, sim: AsyncClient) -> None:
        self.factory = factory
        self.sim_app = sim_app
        self.sim = sim
        self.clock = FakeClock(datetime.now(UTC))
        self.settings = get_settings().model_copy(
            update={"PSP_MAX_ATTEMPTS": 4, "RECON_SETTLEMENT_WINDOW_DAYS": 0}
        )
        self.psp = PspClient(sim, timeout_seconds=0.2)
        self.charger = Charger(
            factory, self.psp, self.clock, self.settings, random.Random(1)
        )
        self.recon = Reconciler(factory, self.psp, self.clock, self.settings)

    def reconciler(self, **overrides: Any) -> Reconciler:
        settings = self.settings.model_copy(update=overrides)
        return Reconciler(self.factory, self.psp, self.clock, settings)

    async def faults(self, **profile: Any) -> None:
        resp = await self.sim.put("/admin/faults", json=profile)
        assert resp.status_code == 200, resp.text

    async def inject(self, day: date, *faults: dict[str, Any]) -> None:
        resp = await self.sim.post(
            f"/admin/settlements/{day}/faults", json={"faults": list(faults)}
        )
        assert resp.status_code == 200, resp.text

    def today(self) -> date:
        """The simulator's settlement date for what it charged."""
        charges = self.sim_app.state.store.ground_truth()["charges"]
        return date.fromisoformat(charges[-1]["created_at"][:10])

    async def captured(self, creds: Any, amount: int = 1_000) -> uuid.UUID:
        payment_id, event = await self._payment(creds, amount)
        assert await self.charger.handle(event) is Result.CAPTURED
        return payment_id

    async def _payment(self, creds: Any, amount: int) -> tuple[uuid.UUID, ChargerEvent]:
        async with self.factory() as db:
            result = await create_payment(
                db,
                tenant_id=creds.tenant_id,
                idempotency_key=str(uuid.uuid4()),
                amount=amount,
                currency="USD",
                wallet_id=creds.wallet_id,
            )
            await db.commit()
        assert result.resource_id is not None
        payload = {
            "payment_id": str(result.resource_id),
            "tenant_id": str(creds.tenant_id),
        }
        return result.resource_id, ChargerEvent(
            "payment.initiated", payload, uuid.uuid4()
        )

    async def refunded(
        self, creds: Any, payment_id: uuid.UUID
    ) -> tuple[uuid.UUID, Result]:
        async with self.factory() as db:
            result = await request_refund(
                db, tenant_id=creds.tenant_id, payment_id=payment_id
            )
            await db.commit()
        assert result.resource_id is not None
        event = ChargerEvent(
            "refund.created",
            {"refund_id": str(result.resource_id), "tenant_id": str(creds.tenant_id)},
            uuid.uuid4(),
        )
        return result.resource_id, await self.charger.handle(event)

    async def row(self, model: Any, row_id: uuid.UUID) -> Any:
        async with self.factory() as db:
            return await db.get(model, row_id)

    def psp_reference(self, payment_id: uuid.UUID) -> str:
        truth = self.sim_app.state.store.ground_truth()["charges"]
        [charge] = [c for c in truth if c["merchant_reference"] == str(payment_id)]
        return str(charge["id"])

    async def items(self, run: ReconRun) -> list[ReconItem]:
        async with self.factory() as db:
            rows = await db.execute(select(ReconItem).where(ReconItem.run_id == run.id))
            return list(rows.scalars().all())

    async def by_payment(self, run: ReconRun) -> dict[uuid.UUID | None, list[str]]:
        found: dict[uuid.UUID | None, list[str]] = {}
        for item in await self.items(run):
            found.setdefault(item.payment_id, []).append(item.classification.value)
        return {k: sorted(v) for k, v in found.items()}

    async def transfer_count(self) -> int:
        async with self.factory() as db:
            return int(
                (
                    await db.execute(select(func.count()).select_from(Transfer))
                ).scalar_one()
            )

    async def kinds(self, reference_id: uuid.UUID) -> list[str]:
        async with self.factory() as db:
            rows = await db.execute(
                select(Transfer.kind).where(Transfer.reference_id == reference_id)
            )
            return sorted(rows.scalars().all())

    async def wallet(self, creds: Any) -> tuple[int, int]:
        async with self.factory() as db:
            wallet = await db.get(Wallet, creds.wallet_id)
            accounts = wallet_accounts(wallet)
            amounts = await balances(db, [accounts.pending, accounts.available])
            return amounts[accounts.pending], amounts[accounts.available]

    async def clean(self, creds: Any, *, settled: bool = True) -> None:
        async with self.factory() as db:
            assert await check_ledger(db, creds.tenant_id) == []
            if settled:
                assert await i7_clearing_accounts_settled(db, creds.tenant_id) == []


@pytest_asyncio.fixture
async def env(session_factory: Factory) -> AsyncIterator[Env]:
    sim_app = create_app(db_path=":memory:", response_timeout_seconds=1.0)
    async with AsyncClient(
        transport=ASGITransport(app=sim_app), base_url="http://sim"
    ) as sim:
        yield Env(session_factory, sim_app, sim)


# --- exit gate 1: a clean day ---------------------------------------------------------


async def test_clean_day_matches_everything_and_clears_psp_clearing(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    kept = [await env.captured(creds, amount) for amount in (1_000, 2_500, 777)]
    refunded = await env.captured(creds, 4_000)
    refund_id, result = await env.refunded(creds, refunded)
    assert result is Result.REFUNDED

    async with env.factory() as db:  # before: everything sits in psp_clearing
        assert await i7_clearing_accounts_settled(db, creds.tenant_id) != []

    run = await env.recon.run(env.today())

    assert run.status is RunStatus.COMPLETED
    assert run.unexplained == 0
    assert run.counts == {"matched": 5}  # 4 charge lines + 1 refund line
    for payment_id in kept:
        payment = await env.row(Payment, payment_id)
        assert payment.status is PaymentStatus.SETTLED
        assert payment.settled_at is not None and payment.settlement_line_id is not None
        assert await env.kinds(payment_id) == [
            "capture",
            "release",
            "settlement_charge",
        ]
    refunded_payment = await env.row(Payment, refunded)
    assert refunded_payment.status is PaymentStatus.REFUNDED  # not SETTLED
    assert await env.kinds(refunded) == ["capture", "settlement_charge"]  # no release
    assert await env.kinds(refund_id) == [
        "refund_confirm",
        "refund_hold",
        "settlement_refund",
    ]

    pending, available = await env.wallet(creds)
    assert (pending, available) == (0, 1_000 + 2_500 + 777)
    async with env.factory() as db:
        system = await _system_balances(db, creds.tenant_id)
    fees = sum(fee_for(a) for a in (1_000, 2_500, 777, 4_000))
    assert system[AccountKind.PSP_FEES] == fees
    assert system[AccountKind.BANK] == 1_000 + 2_500 + 777 - fees  # refund paid back
    await env.clean(creds)  # I1-I6 and I7


async def _system_balances(
    db: AsyncSession, tenant_id: uuid.UUID
) -> dict[AccountKind, int]:
    from app.services.ledger import system_accounts

    ids = await system_accounts(db, tenant_id=tenant_id, currency="USD")
    amounts = await balances(db, ids.values())
    return {kind: amounts[account_id] for kind, account_id in ids.items()}


# --- exit gate 2: every discrepancy type is classified --------------------------------


async def test_each_injected_discrepancy_is_detected_and_classified(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    skewed, omitted, doubled, fine = [await env.captured(creds) for _ in range(4)]
    day = env.today()
    await env.inject(
        day,
        {"kind": "amount_mismatch", "psp_reference": env.psp_reference(skewed)},
        {"kind": "missing", "psp_reference": env.psp_reference(omitted)},
        {"kind": "duplicate", "psp_reference": env.psp_reference(doubled)},
        {"kind": "extra"},
    )

    run = await env.recon.run(day)

    assert await env.by_payment(run) == {
        skewed: ["amount_mismatch"],
        omitted: ["missing_at_psp"],
        doubled: ["duplicate_line", "matched"],
        fine: ["matched"],
        None: ["missing_in_ledger"],  # the extra line names no payment of ours
    }
    assert run.unexplained == 4
    items = {i.classification: i for i in await env.items(run)}
    mismatch = items[Classification.AMOUNT_MISMATCH]
    assert (mismatch.expected, mismatch.actual) == (1_000, 1_100)
    assert "gross 1100 != 1000" in (mismatch.detail or "")

    # Flagged rows are reported, never posted or corrected
    for payment_id in (skewed, omitted):
        payment = await env.row(Payment, payment_id)
        assert payment.status is PaymentStatus.CAPTURED
        assert await env.kinds(payment_id) == ["capture"]
    assert await env.kinds(doubled) == ["capture", "release", "settlement_charge"]
    await env.clean(creds, settled=False)  # balanced, but clearing is not zero:
    async with env.factory() as db:  # the two unsettled captures are still owed
        [violation] = await i7_clearing_accounts_settled(db, creds.tenant_id)
        assert "psp_clearing USD" in violation and " 2000 " in violation


# --- exit gate 3: re-running a date posts nothing --------------------------------------


async def test_running_the_same_date_twice_posts_nothing_the_second_time(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_ids = [await env.captured(creds) for _ in range(3)]
    await env.inject(
        env.today(),
        {"kind": "duplicate", "psp_reference": env.psp_reference(payment_ids[0])},
    )

    first = await env.recon.run(env.today())
    transfers = await env.transfer_count()
    second = await env.recon.run(env.today())

    assert first.posted == 6 and second.posted == 0  # settlement + release each
    assert await env.transfer_count() == transfers
    assert second.counts == first.counts == {"duplicate_line": 1, "matched": 3}
    assert second.id != first.id  # each run is its own report
    await env.clean(creds)


# --- exit gate 4: healing what nothing else reported ------------------------------------


async def test_charge_then_timeout_with_a_dropped_webhook_is_healed(
    env: Env, make_tenant: Any
) -> None:
    """The provider charged, the response was lost, the webhook never came
    and the sweeper has not run: only the settlement file knows."""
    creds = await make_tenant()
    await env.faults(charge_then_timeout_rate=1.0)
    payment_id, event = await env._payment(creds, 1_500)
    assert await env.charger.handle(event) is Result.RETRY_SCHEDULED
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.PROCESSING

    run = await env.recon.run(env.today())

    assert await env.by_payment(run) == {payment_id: ["healed"]}
    assert run.unexplained == 0
    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.SETTLED
    assert payment.psp_reference == env.psp_reference(payment_id)
    assert payment.next_attempt_at is None  # the sweeper will leave it alone
    assert await env.kinds(payment_id) == ["capture", "release", "settlement_charge"]
    async with env.factory() as db:
        events = await db.execute(
            text(
                "SELECT payload->>'source' FROM outbox_events "
                "WHERE event_type = 'payment.captured' AND payload->>'payment_id' = :p"
            ),
            {"p": str(payment_id)},
        )
        assert events.scalars().all() == ["reconciliation"]
    assert (await env.wallet(creds)) == (0, 1_500)
    await env.clean(creds)


async def test_refund_whose_response_was_lost_is_healed(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await env.captured(creds, 2_000)
    await env.faults(refund_then_timeout_rate=1.0)
    refund_id, result = await env.refunded(creds, payment_id)
    assert result is Result.RETRY_SCHEDULED

    run = await env.recon.run(env.today())

    assert sorted(i.classification.value for i in await env.items(run)) == [
        "healed",
        "matched",
    ]
    refund = await env.row(Refund, refund_id)
    assert refund.status is RefundStatus.SUCCEEDED and refund.settlement_line_id
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.REFUNDED
    assert (await env.wallet(creds)) == (0, 0)
    await env.clean(creds)


# --- edges ------------------------------------------------------------------------------


async def test_settling_while_a_refund_is_in_flight_skips_the_release(
    env: Env, make_tenant: Any
) -> None:
    """The refund hold already took the money out of pending; releasing too
    would pay the merchant twice. If the refund later fails, its hold goes to
    available because the payment is SETTLED by then."""
    creds = await make_tenant()
    payment_id = await env.captured(creds, 3_000)
    async with env.factory() as db:
        result = await request_refund(
            db, tenant_id=creds.tenant_id, payment_id=payment_id
        )
        await db.commit()
    refund_id = result.resource_id

    run = await env.recon.run(env.today())
    assert run.counts == {"matched": 1}
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.SETTLED
    assert await env.kinds(payment_id) == ["capture", "settlement_charge"]

    await env.faults(refund_fail_rate=1.0)
    event = ChargerEvent(
        "refund.created",
        {"refund_id": str(refund_id), "tenant_id": str(creds.tenant_id)},
        uuid.uuid4(),
    )
    assert await env.charger.handle(event) is Result.REFUND_FAILED
    assert (await env.wallet(creds)) == (0, 3_000)
    await env.clean(creds)


async def test_window_defers_missing_at_psp_to_the_run_that_closes_it(
    env: Env, make_tenant: Any
) -> None:
    """With a one-day window, today's capture missing from today's file is
    not yet suspicious; tomorrow's run reports it."""
    creds = await make_tenant()
    late = await env.captured(creds)
    today = env.today()
    await env.inject(
        today, {"kind": "missing", "psp_reference": env.psp_reference(late)}
    )
    windowed = env.reconciler(RECON_SETTLEMENT_WINDOW_DAYS=1)

    assert (await windowed.run(today)).unexplained == 0
    tomorrow = await windowed.run(today + timedelta(days=1))  # empty file
    assert await env.by_payment(tomorrow) == {late: ["missing_at_psp"]}


async def test_conflicts_and_bad_lines_are_reported_not_posted(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(decline_rate=1.0)
    failed_id, event = await env._payment(creds, 900)
    assert await env.charger.handle(event) is Result.DECLINED
    pending_id, _ = await env._payment(creds, 800)
    day = date(2030, 1, 1)
    content = (
        "line_id,type,psp_reference,merchant_reference,gross,fee,net,currency\n"
        f"a,charge,{env.psp_reference(failed_id)},{failed_id},900,56,844,USD\n"
        f"b,charge,ch_p,{pending_id},800,53,747,USD\n"
        f"c,charge,ch_n,{pending_id},800,53,700,USD\n"
        f"d,charge,ch_e,{pending_id},800,53,747,EUR\n"
        "e,refund,re_x,not-a-uuid,100,0,100,USD\n"
        f"f,charge,ch_other,{failed_id},900,56,844,USD\n"
    )
    run_id = await env.recon._start(day)
    lines = await env.recon.load_file(day, content)
    verdicts = [await env.recon.reconcile_line(run_id, line) for line in lines]

    got = [(v.classification.value, v.detail) for v in verdicts]
    assert got[0] == (
        "status_conflict",
        "provider settled a charge for a FAILED payment",
    )
    assert got[1] == (
        "status_conflict",
        "provider settled a charge for a PENDING payment",
    )
    assert got[2][0] == "amount_mismatch" and "net 700" in (got[2][1] or "")
    assert got[3][0] == "amount_mismatch" and "currency EUR" in (got[3][1] or "")
    assert got[4][0] == "missing_in_ledger"
    assert got[5][0] == "status_conflict" and "recorded charge" in (got[5][1] or "")
    assert await env.kinds(failed_id) == [] and await env.kinds(pending_id) == []
    again = await env.recon.load_file(day, content)
    assert [line.id for line in again] == [line.id for line in lines]  # idempotent load


async def test_malformed_file_fails_the_run_and_loads_nothing(env: Env) -> None:
    with pytest.raises(SettlementFileError, match="line 2"):
        await env.recon.load_file(
            date(2030, 1, 2),
            "line_id,type,psp_reference,"
            "merchant_reference,gross,fee,net,currency\nx,charge,ch,m,ten,0,0,USD\n",
        )
    with pytest.raises(SettlementFileError, match="missing columns"):
        await env.recon.load_file(date(2030, 1, 2), "a,b\n1,2\n")


async def test_crash_mid_file_resumes_cleanly_on_rerun(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_ids = [await env.captured(creds) for _ in range(3)]

    with armed("recon.after_line"), pytest.raises(SimulatedCrash):
        await env.recon.run(env.today())  # dies after the first line commits
    settled = [(await env.row(Payment, p)).status for p in payment_ids]
    assert settled.count(PaymentStatus.SETTLED) == 1

    run = await env.recon.run(env.today())
    assert run.counts == {"matched": 3} and run.posted == 4  # the rest only
    async with env.factory() as db:
        statuses = (
            (await db.execute(select(ReconRun.status).order_by(ReconRun.started_at)))
            .scalars()
            .all()
        )
    assert statuses == [RunStatus.ABANDONED, RunStatus.COMPLETED]
    await env.clean(creds)


async def test_a_second_run_of_the_same_date_waits_its_turn(
    env: Env, owner_database_url: str
) -> None:
    from app.services.reconciliation import _LOCK_CLASS

    day = date(2030, 1, 3)
    engine = create_async_engine(owner_database_url)
    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT pg_advisory_lock(:c, :d)"),
            {"c": _LOCK_CLASS, "d": day.toordinal()},
        )
        with pytest.raises(ReconInProgressError):
            await env.recon.run(day)
        await conn.execute(
            text("SELECT pg_advisory_unlock(:c, :d)"),
            {"c": _LOCK_CLASS, "d": day.toordinal()},
        )
    await engine.dispose()
    assert (await env.recon.run(day)).status is RunStatus.COMPLETED


async def test_settlement_line_is_set_once(
    env: Env, make_tenant: Any, owner_database_url: str
) -> None:
    creds = await make_tenant()
    payment_id = await env.captured(creds)
    await env.recon.run(env.today())
    other = await env.recon.load_file(
        date(2030, 1, 4),
        "line_id,type,psp_reference,merchant_reference,gross,fee,net,currency\n"
        "z,charge,ch_z,m,1,0,1,USD\n",
    )
    async with env.factory() as db:
        with pytest.raises(DBAPIError, match="settlement line is set once"):
            await db.execute(
                text("UPDATE payments SET settlement_line_id = :l WHERE id = :p"),
                {"l": other[0].id, "p": payment_id},
            )


# --- API --------------------------------------------------------------------------------


@pytest.fixture
def recon_api(
    env: Env, api_client: Callable[..., Any], session_factory: Factory
) -> Callable[..., Any]:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _client(psp: PspClient | None = None) -> AsyncIterator[AsyncClient]:
        async with api_client(RECON_SETTLEMENT_WINDOW_DAYS=0) as client:
            from app.core.clock import get_clock
            from app.dependencies import get_psp_client, get_session_factory
            from app.main import app

            app.dependency_overrides[get_session_factory] = lambda: session_factory
            app.dependency_overrides[get_psp_client] = lambda: psp or env.psp
            app.dependency_overrides[get_clock] = lambda: env.clock
            yield client

    return _client


async def _platform_key(factory: Factory) -> dict[str, str]:
    from app.cli import create_api_key
    from tests.conftest import DEFAULT_TENANT_ID

    async with factory() as db:
        key = await create_api_key(db, DEFAULT_TENANT_ID)
        await db.commit()
    return {"Authorization": f"Bearer {key}"}


async def test_api_runs_and_reports_a_reconciliation(
    env: Env, make_tenant: Any, recon_api: Callable[..., Any]
) -> None:
    creds = await make_tenant()
    skewed = await env.captured(creds)
    await env.captured(creds)
    await env.inject(
        env.today(),
        {"kind": "amount_mismatch", "psp_reference": env.psp_reference(skewed)},
    )
    platform = await _platform_key(env.factory)

    async with recon_api() as api:
        resp = await api.post(
            "/v1/reconciliations", json={"date": str(env.today())}, headers=platform
        )
        assert resp.status_code == 201, resp.text
        run = resp.json()
        assert run["counts"] == {"amount_mismatch": 1, "matched": 1}
        assert run["unexplained"] == 1 and run["status"] == "completed"
        assert run["items"] is None

        detail = await api.get(f"/v1/reconciliations/{run['id']}", headers=platform)
        assert detail.status_code == 200
        assert len(detail.json()["items"]) == 2
        unexplained = await api.get(
            f"/v1/reconciliations/{run['id']}?unexplained_only=true", headers=platform
        )
        [item] = unexplained.json()["items"]
        assert item["classification"] == "amount_mismatch"
        assert item["payment_id"] == str(skewed) and item["line_id"]
        matched = await api.get(
            f"/v1/reconciliations/{run['id']}?classification=matched", headers=platform
        )
        assert [i["classification"] for i in matched.json()["items"]] == ["matched"]

        assert (
            await api.get(f"/v1/reconciliations/{uuid.uuid4()}", headers=platform)
        ).status_code == 404
        # A merchant's key can do neither
        assert (
            await api.post(
                "/v1/reconciliations",
                json={"date": "2030-01-01"},
                headers=creds.headers,
            )
        ).status_code == 403
        assert (
            await api.get(f"/v1/reconciliations/{run['id']}", headers=creds.headers)
        ).status_code == 403


async def test_api_reports_an_unreachable_provider_as_502(
    env: Env, recon_api: Callable[..., Any]
) -> None:
    import httpx

    broken = PspClient(
        AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503)),
            base_url="http://x",
        ),
        timeout_seconds=0.2,
    )
    platform = await _platform_key(env.factory)
    async with recon_api(psp=broken) as api:
        resp = await api.post(
            "/v1/reconciliations", json={"date": "2030-01-05"}, headers=platform
        )
    assert resp.status_code == 502
    async with env.factory() as db:
        [run] = (await db.execute(select(ReconRun))).scalars().all()
    assert run.status is RunStatus.FAILED and "503" in (run.error or "")
