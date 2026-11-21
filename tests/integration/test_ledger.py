"""
Stage 2 exit gate: the ledger keeps itself honest.

Against real PostgreSQL, running as the restricted application role unless a
test says otherwise. Invariant checks come from verification/invariants.py.
"""

import asyncio
import uuid
from collections import Counter
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db_errors import pg_sqlstate
from app.models.ledger import AccountKind, Entry, Transfer
from app.models.wallet import Wallet
from app.services.ledger import (
    InsufficientFundsError,
    LedgerError,
    Line,
    Reference,
    balances,
    credit,
    debit,
    post_transfer,
    system_accounts,
)
from app.services.wallets import create_wallet
from verification.invariants import check_ledger

pytestmark = pytest.mark.asyncio
Factory = async_sessionmaker[AsyncSession]
DEADLOCK = "40P01"


async def _wallet(
    factory: Factory, tenant_id: uuid.UUID, currency: str = "USD"
) -> Wallet:
    async with factory() as db:
        result = await create_wallet(db, tenant_id=tenant_id, currency=currency)
        await db.commit()
        wallet = await db.get(Wallet, result.resource_id)
        assert wallet is not None
        return wallet


async def _fund(
    factory: Factory, tenant_id: uuid.UUID, wallet: Wallet, amount: int
) -> None:
    """Test-only money in: provider clearing -> wallet available."""
    async with factory() as db:
        system = await system_accounts(
            db, tenant_id=tenant_id, currency=wallet.currency
        )
        await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference("test_funding", "test", uuid.uuid4()),
            lines=[
                debit(system[AccountKind.PSP_CLEARING], amount),
                credit(wallet.available_account_id, amount),
            ],
        )
        await db.commit()


async def _move(
    factory: Factory, tenant_id: uuid.UUID, src: Wallet, dst: Wallet, amount: int
) -> str:
    async with factory() as db:
        try:
            await post_transfer(
                db,
                tenant_id=tenant_id,
                reference=Reference("wallet_transfer", "test", uuid.uuid4()),
                lines=[
                    debit(src.available_account_id, amount),
                    credit(dst.available_account_id, amount),
                ],
            )
            await db.commit()
            return "ok"
        except InsufficientFundsError:
            await db.rollback()
            return "insufficient"
        except DBAPIError as exc:
            await db.rollback()
            return "deadlock" if pg_sqlstate(exc) == DEADLOCK else f"error {exc}"


async def _available(factory: Factory, wallet: Wallet) -> int:
    async with factory() as db:
        return (await balances(db, [wallet.available_account_id]))[
            wallet.available_account_id
        ]


async def _violations(
    factory: Factory, tenant_id: uuid.UUID | None = None
) -> list[str]:
    async with factory() as db:
        return await check_ledger(db, tenant_id)


# --- Property: random transfers keep I1, I2, I5, I6 ---------------------------------

transfer_plans = st.lists(
    st.tuples(st.integers(0, 3), st.integers(0, 3), st.integers(1, 400)).filter(
        lambda t: t[0] != t[1]
    ),
    min_size=1,
    max_size=25,
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    plan=transfer_plans, funding=st.lists(st.integers(0, 500), min_size=4, max_size=4)
)
async def test_random_transfers_preserve_ledger_invariants(
    session_factory: Factory,
    make_tenant: Any,
    plan: list[tuple[int, int, int]],
    funding: list[int],
) -> None:
    creds = await make_tenant()  # fresh tenant per example: no shared state
    tenant = creds.tenant_id
    wallets = [await _wallet(session_factory, tenant) for _ in range(4)]
    model = list(funding)
    for wallet, amount in zip(wallets, funding, strict=True):
        if amount:
            await _fund(session_factory, tenant, wallet, amount)

    for src, dst, amount in plan:
        outcome = await _move(
            session_factory, tenant, wallets[src], wallets[dst], amount
        )
        if model[src] >= amount:
            assert outcome == "ok"
            model[src] -= amount
            model[dst] += amount
        else:
            assert outcome == "insufficient"

    assert [await _available(session_factory, w) for w in wallets] == model
    assert await _violations(session_factory, tenant) == []


# --- Database-level rules -------------------------------------------------------------


async def test_unbalanced_transfer_fails_at_commit(
    session_factory: Factory, make_tenant: Any
) -> None:
    """Bypass the service: the deferred trigger rejects it at COMMIT, not before."""
    tenant = (await make_tenant()).tenant_id
    a, b = (
        await _wallet(session_factory, tenant),
        await _wallet(session_factory, tenant),
    )
    async with session_factory() as db:
        transfer = Transfer(
            id=uuid.uuid4(),
            tenant_id=tenant,
            kind="manual",
            reference_type="test",
            reference_id=uuid.uuid4(),
            currency="USD",
        )
        db.add(transfer)
        await db.flush()
        db.add_all(
            [
                Entry(
                    transfer_id=transfer.id,
                    account_id=a.pending_account_id,
                    direction="debit",
                    amount=100,
                    currency="USD",
                ),
                Entry(
                    transfer_id=transfer.id,
                    account_id=b.pending_account_id,
                    direction="credit",
                    amount=90,
                    currency="USD",
                ),
            ]
        )
        await db.flush()  # statements succeed; the check is deferred
        with pytest.raises(DBAPIError, match="unbalanced"):
            await db.commit()


@pytest.mark.parametrize("entries", [0, 1])
async def test_transfer_needs_at_least_two_entries(
    session_factory: Factory, make_tenant: Any, entries: int
) -> None:
    tenant = (await make_tenant()).tenant_id
    a = await _wallet(session_factory, tenant)
    async with session_factory() as db:
        transfer = Transfer(
            id=uuid.uuid4(),
            tenant_id=tenant,
            kind="manual",
            reference_type="test",
            reference_id=uuid.uuid4(),
            currency="USD",
        )
        db.add(transfer)
        await db.flush()
        if entries:
            db.add(
                Entry(
                    transfer_id=transfer.id,
                    account_id=a.pending_account_id,
                    direction="debit",
                    amount=5,
                    currency="USD",
                )
            )
            await db.flush()
        with pytest.raises(DBAPIError, match="at least 2 required"):
            await db.commit()


async def test_entry_currency_must_match_account(
    session_factory: Factory, make_tenant: Any
) -> None:
    tenant = (await make_tenant()).tenant_id
    usd = await _wallet(session_factory, tenant, "USD")
    async with session_factory() as db:
        transfer = Transfer(
            id=uuid.uuid4(),
            tenant_id=tenant,
            kind="manual",
            reference_type="test",
            reference_id=uuid.uuid4(),
            currency="NGN",
        )
        db.add(transfer)
        await db.flush()
        db.add(
            Entry(
                transfer_id=transfer.id,
                account_id=usd.pending_account_id,
                direction="debit",
                amount=5,
                currency="NGN",
            )
        )
        with pytest.raises(IntegrityError, match="fk_entries_account_currency"):
            await db.flush()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE entries SET amount = amount + 1",
        "DELETE FROM entries",
        "TRUNCATE entries CASCADE",
        "UPDATE transfers SET kind = 'x'",
        "DELETE FROM transfers",
    ],
)
async def test_app_role_cannot_change_ledger_rows(
    session_factory: Factory, make_tenant: Any, statement: str
) -> None:
    tenant = (await make_tenant()).tenant_id
    await _fund(session_factory, tenant, await _wallet(session_factory, tenant), 10)
    async with session_factory() as db:
        with pytest.raises(DBAPIError, match="permission denied"):
            await db.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE entries SET amount = amount + 1",
        "DELETE FROM entries",
        "TRUNCATE entries CASCADE",
        "UPDATE transfers SET kind = 'x'",
        "DELETE FROM transfers",
    ],
)
async def test_even_the_owner_cannot_change_ledger_rows(
    session_factory: Factory,
    owner_session_factory: Factory,
    make_tenant: Any,
    statement: str,
) -> None:
    """Privileges do not bind a table's owner; the triggers do."""
    tenant = (await make_tenant()).tenant_id
    await _fund(session_factory, tenant, await _wallet(session_factory, tenant), 10)
    async with owner_session_factory() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text(statement))


async def test_guarded_account_cannot_go_negative_even_via_raw_sql(
    session_factory: Factory, make_tenant: Any
) -> None:
    tenant = (await make_tenant()).tenant_id
    wallet = await _wallet(session_factory, tenant)
    async with session_factory() as db:
        with pytest.raises(IntegrityError, match="ck_accounts_guard"):
            await db.execute(
                text("UPDATE accounts SET debits_posted = 1 WHERE id = :id"),
                {"id": wallet.available_account_id},
            )


# --- Service behaviour ----------------------------------------------------------------


async def test_posting_the_same_reference_twice_writes_once(
    session_factory: Factory, make_tenant: Any
) -> None:
    tenant = (await make_tenant()).tenant_id
    a, b = (
        await _wallet(session_factory, tenant),
        await _wallet(session_factory, tenant),
    )
    await _fund(session_factory, tenant, a, 100)
    reference = Reference("wallet_transfer", "test", uuid.uuid4())
    lines = [debit(a.available_account_id, 30), credit(b.available_account_id, 30)]

    async with session_factory() as db:
        first = await post_transfer(
            db, tenant_id=tenant, reference=reference, lines=lines
        )
        await db.commit()
    async with session_factory() as db:
        second = await post_transfer(
            db, tenant_id=tenant, reference=reference, lines=lines
        )
        await db.commit()
        entries = (
            await db.execute(
                select(func.count())
                .select_from(Entry)
                .where(Entry.transfer_id == first.transfer.id)
            )
        ).scalar_one()

    assert first.created and not second.created
    assert second.transfer.id == first.transfer.id
    assert entries == 2
    assert await _available(session_factory, a) == 70
    assert await _available(session_factory, b) == 30


@pytest.mark.parametrize(
    "lines_factory,message",
    [
        (lambda a, b: [debit(a, 10)], "at least two"),
        (lambda a, b: [debit(a, 10), credit(b, 9)], "unbalanced"),
        (lambda a, b: [debit(a, 0), credit(b, 0)], "positive int"),
        (lambda a, b: [debit(a, 1.5), credit(b, 1.5)], "positive int"),  # type: ignore[arg-type]
    ],
)
async def test_invalid_postings_are_rejected_before_any_write(
    session_factory: Factory, make_tenant: Any, lines_factory: Any, message: str
) -> None:
    tenant = (await make_tenant()).tenant_id
    a, b = (
        await _wallet(session_factory, tenant),
        await _wallet(session_factory, tenant),
    )
    lines: list[Line] = lines_factory(a.available_account_id, b.available_account_id)
    async with session_factory() as db:
        with pytest.raises(LedgerError, match=message):
            await post_transfer(
                db,
                tenant_id=tenant,
                reference=Reference("x", "t", uuid.uuid4()),
                lines=lines,
            )


async def test_cross_currency_and_cross_tenant_postings_are_rejected(
    session_factory: Factory, make_tenant: Any
) -> None:
    alice, bob = (await make_tenant("a")).tenant_id, (await make_tenant("b")).tenant_id
    usd, ngn = (
        await _wallet(session_factory, alice, "USD"),
        await _wallet(session_factory, alice, "NGN"),
    )
    bobs = await _wallet(session_factory, bob, "USD")
    async with session_factory() as db:
        with pytest.raises(LedgerError, match="one currency"):
            await post_transfer(
                db,
                tenant_id=alice,
                reference=Reference("x", "t", uuid.uuid4()),
                lines=[
                    debit(usd.available_account_id, 1),
                    credit(ngn.available_account_id, 1),
                ],
            )
        with pytest.raises(LedgerError, match="not found for tenant"):
            await post_transfer(
                db,
                tenant_id=alice,
                reference=Reference("x", "t", uuid.uuid4()),
                lines=[
                    debit(usd.available_account_id, 1),
                    credit(bobs.available_account_id, 1),
                ],
            )


async def test_system_accounts_are_created_once_per_tenant_and_currency(
    session_factory: Factory, make_tenant: Any
) -> None:
    tenant = (await make_tenant()).tenant_id
    results = await asyncio.gather(
        *[_wallet(session_factory, tenant, "EUR") for _ in range(10)]
    )
    assert len({w.id for w in results}) == 10
    async with session_factory() as db:
        kinds = (
            await db.execute(
                text(
                    "SELECT kind, count(*) FROM accounts WHERE tenant_id = :t "
                    "AND currency = 'EUR' AND wallet_id IS NULL GROUP BY kind"
                ),
                {"t": tenant},
            )
        ).all()
    assert dict(kinds) == {
        "psp_clearing": 1,
        "bank": 1,
        "psp_fees": 1,
        "refunds_payable": 1,
    }


# --- Exit gate: opposing concurrent transfers never deadlock or overdraw -------------


async def test_opposing_concurrent_transfers_never_deadlock(
    session_factory: Factory, make_tenant: Any
) -> None:
    tenant = (await make_tenant()).tenant_id
    a, b = (
        await _wallet(session_factory, tenant),
        await _wallet(session_factory, tenant),
    )
    await _fund(session_factory, tenant, a, 1_000)
    await _fund(session_factory, tenant, b, 1_000)

    outcomes: Counter[str] = Counter()
    for _ in range(10):  # 10 rounds x 100 opposing pairs = 1,000 pairs
        results = await asyncio.gather(
            *[
                coro
                for _ in range(100)
                for coro in (
                    _move(session_factory, tenant, a, b, 1),
                    _move(session_factory, tenant, b, a, 1),
                )
            ]
        )
        outcomes.update(results)

    assert outcomes == {"ok": 2_000}, outcomes
    assert await _available(session_factory, a) == 1_000
    assert await _available(session_factory, b) == 1_000
    assert await _violations(session_factory, tenant) == []


async def test_concurrent_overdraft_attempts_never_overdraw(
    session_factory: Factory, make_tenant: Any
) -> None:
    tenant = (await make_tenant()).tenant_id
    a, b = (
        await _wallet(session_factory, tenant),
        await _wallet(session_factory, tenant),
    )
    await _fund(session_factory, tenant, a, 10)

    results = Counter(
        await asyncio.gather(
            *[_move(session_factory, tenant, a, b, 1) for _ in range(200)]
        )
    )
    assert results == {"ok": 10, "insufficient": 190}, results
    assert await _available(session_factory, a) == 0
    assert await _available(session_factory, b) == 10
    assert await _violations(session_factory, tenant) == []
