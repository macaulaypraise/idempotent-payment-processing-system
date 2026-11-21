"""Posting service for the double-entry ledger (ADR 0002, ADR 0003).

`post_transfer` is the only function that writes ledger rows. It runs inside
the caller's transaction and never commits.

Rules:
- amounts are positive integers in minor units; one currency per transfer;
  debits equal credits (the database re-checks all of this at COMMIT)
- account totals are updated with one in-place increment per account, in
  ascending account id order, so concurrent transfers lock accounts in the
  same order and cannot deadlock (ADR 0003)
- a transfer is unique on (tenant, kind, reference_type, reference_id):
  posting the same reference again writes nothing and returns the original
- a guarded account going negative surfaces as InsufficientFundsError
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_errors import violates_constraint
from app.core.metrics import ledger_transfers_total
from app.models.ledger import (
    NORMAL_SIDE,
    SYSTEM_KINDS,
    Account,
    AccountKind,
    Entry,
    Side,
    Transfer,
)


class LedgerError(Exception):
    """A posting request that can never be valid (a programming error)."""


class InsufficientFundsError(Exception):
    """A guarded account would have gone below zero."""

    def __init__(self, account_ids: Sequence[uuid.UUID] = ()) -> None:
        super().__init__("insufficient funds")
        self.account_ids = list(account_ids)


@dataclass(frozen=True)
class Line:
    account_id: uuid.UUID
    side: Side
    amount: int


@dataclass(frozen=True)
class Reference:
    kind: str
    reference_type: str
    reference_id: uuid.UUID


@dataclass(frozen=True)
class PostedTransfer:
    transfer: Transfer
    created: bool  # False when the reference had already been posted


def debit(account_id: uuid.UUID, amount: int) -> Line:
    return Line(account_id, Side.DEBIT, amount)


def credit(account_id: uuid.UUID, amount: int) -> Line:
    return Line(account_id, Side.CREDIT, amount)


def _validate(lines: Sequence[Line]) -> None:
    if len(lines) < 2:
        raise LedgerError("a transfer needs at least two lines")
    for line in lines:
        if type(line.amount) is not int or line.amount <= 0:
            raise LedgerError(f"amount must be a positive int, got {line.amount!r}")
    debits = sum(line.amount for line in lines if line.side is Side.DEBIT)
    credits = sum(line.amount for line in lines if line.side is Side.CREDIT)
    if debits != credits:
        raise LedgerError(f"unbalanced transfer: debits {debits} != credits {credits}")


async def post_transfer(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    reference: Reference,
    lines: Sequence[Line],
) -> PostedTransfer:
    """Post a balanced transfer, or return the one already posted for
    `reference`. Must run inside a transaction; does not commit."""
    _validate(lines)

    account_ids = sorted({line.account_id for line in lines})
    accounts = {
        row.id: row
        for row in (
            await db.execute(
                select(Account.id, Account.currency, Account.tenant_id).where(
                    Account.id.in_(account_ids)
                )
            )
        ).all()
    }
    missing = [a for a in account_ids if a not in accounts]
    if missing or any(row.tenant_id != tenant_id for row in accounts.values()):
        raise LedgerError(f"accounts not found for tenant: {missing or account_ids}")
    currencies = {row.currency for row in accounts.values()}
    if len(currencies) != 1:
        raise LedgerError(f"one currency per transfer, got {sorted(currencies)}")
    currency = currencies.pop()

    transfer_id = uuid.uuid4()
    inserted = await db.execute(
        insert(Transfer)
        .values(
            id=transfer_id,
            tenant_id=tenant_id,
            kind=reference.kind,
            reference_type=reference.reference_type,
            reference_id=reference.reference_id,
            currency=currency,
        )
        .on_conflict_do_nothing(constraint="uq_transfers_reference")
        .returning(Transfer.id)
    )
    if inserted.scalar_one_or_none() is None:
        existing = (
            await db.execute(
                select(Transfer).where(
                    Transfer.tenant_id == tenant_id,
                    Transfer.kind == reference.kind,
                    Transfer.reference_type == reference.reference_type,
                    Transfer.reference_id == reference.reference_id,
                )
            )
        ).scalar_one()
        return PostedTransfer(existing, created=False)

    totals: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
    for line in lines:
        totals[line.account_id][0 if line.side is Side.DEBIT else 1] += line.amount

    try:
        for account_id in account_ids:  # ascending id order: no deadlocks
            debit_amount, credit_amount = totals[account_id]
            await db.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(
                    debits_posted=Account.debits_posted + debit_amount,
                    credits_posted=Account.credits_posted + credit_amount,
                )
            )
    except IntegrityError as exc:
        if violates_constraint(exc, "ck_accounts_guard"):
            raise InsufficientFundsError(account_ids) from exc
        raise

    db.add_all(
        Entry(
            transfer_id=transfer_id,
            account_id=line.account_id,
            direction=line.side.value,
            amount=line.amount,
            currency=currency,
        )
        for line in lines
    )
    await db.flush()
    transfer = (
        await db.execute(select(Transfer).where(Transfer.id == transfer_id))
    ).scalar_one()
    # Counted at flush: a later rollback over-counts, which is acceptable for
    # a rate metric; correctness checks use the database, not this counter.
    ledger_transfers_total.labels(kind=reference.kind).inc()
    return PostedTransfer(transfer, created=True)


def signed_balance(normal_side: str, debits_posted: int, credits_posted: int) -> int:
    """Balance on the account's normal side (positive = normal)."""
    if normal_side == Side.DEBIT.value:
        return debits_posted - credits_posted
    return credits_posted - debits_posted


async def balances(
    db: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    rows = (
        await db.execute(
            select(
                Account.id,
                Account.normal_side,
                Account.debits_posted,
                Account.credits_posted,
            ).where(Account.id.in_(list(account_ids)))
        )
    ).all()
    return {
        r.id: signed_balance(r.normal_side, r.debits_posted, r.credits_posted)
        for r in rows
    }


async def system_accounts(
    db: AsyncSession, *, tenant_id: uuid.UUID, currency: str
) -> dict[AccountKind, uuid.UUID]:
    """Ensure the tenant's system accounts for `currency` exist; return ids.

    Created lazily per currency (the first wallet in a currency creates them).
    Concurrent callers race safely on the partial unique index.
    """
    await db.execute(
        insert(Account)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "kind": kind.value,
                    "currency": currency,
                    "normal_side": NORMAL_SIDE[kind].value,
                    "guarded": False,
                    "wallet_id": None,
                }
                for kind in SYSTEM_KINDS
            ]
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "kind", "currency"],
            index_where=Account.wallet_id.is_(None),
        )
    )
    rows = (
        await db.execute(
            select(Account.kind, Account.id).where(
                and_(
                    Account.tenant_id == tenant_id,
                    Account.currency == currency,
                    Account.wallet_id.is_(None),
                )
            )
        )
    ).all()
    return {AccountKind(row.kind): row.id for row in rows}
