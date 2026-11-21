"""Wallets and wallet-to-wallet transfers on top of the ledger."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import NORMAL_SIDE, Account, AccountKind, Entry
from app.models.wallet import Wallet
from app.services import posting_rules
from app.services.idempotency import BusinessRejection, HandlerResult
from app.services.ledger import (
    InsufficientFundsError,
    Reference,
    balances,
    post_transfer,
    system_accounts,
)
from app.services.posting_rules import WalletAccounts

# Wallet transfers are referenced by their idempotency key, so the ledger's
# own unique constraint backs up the idempotency layer.
_KEY_NAMESPACE = uuid.UUID("6f1c1f3e-2a51-4f1a-9a59-4b5b8e1c0d11")


def idempotency_reference(tenant_id: uuid.UUID, key: str) -> uuid.UUID:
    return uuid.uuid5(_KEY_NAMESPACE, f"{tenant_id}:{key}")


def wallet_accounts(wallet: Wallet) -> WalletAccounts:
    return WalletAccounts(wallet.pending_account_id, wallet.available_account_id)


def _wallet_account(
    tenant_id: uuid.UUID, wallet_id: uuid.UUID, kind: AccountKind, currency: str
) -> Account:
    return Account(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=kind.value,
        currency=currency,
        normal_side=NORMAL_SIDE[kind].value,
        guarded=True,
        wallet_id=wallet_id,
    )


async def create_wallet(
    db: AsyncSession, *, tenant_id: uuid.UUID, currency: str
) -> HandlerResult:
    """Business write for POST /v1/wallets (inside the idempotency savepoint)."""
    await system_accounts(db, tenant_id=tenant_id, currency=currency)
    wallet_id = uuid.uuid4()
    pending = _wallet_account(
        tenant_id, wallet_id, AccountKind.WALLET_PENDING, currency
    )
    available = _wallet_account(
        tenant_id, wallet_id, AccountKind.WALLET_AVAILABLE, currency
    )
    db.add_all([pending, available])
    await db.flush()
    wallet = Wallet(
        id=wallet_id,
        tenant_id=tenant_id,
        currency=currency,
        pending_account_id=pending.id,
        available_account_id=available.id,
    )
    db.add(wallet)
    await db.flush()
    await db.refresh(wallet)
    body = {
        "wallet_id": str(wallet.id),
        "currency": currency,
        "available": 0,
        "pending": 0,
        "created_at": wallet.created_at.isoformat(),
    }
    return HandlerResult(status_code=201, body=body, resource_id=wallet.id)


async def get_wallet(
    db: AsyncSession, *, tenant_id: uuid.UUID, wallet_id: uuid.UUID
) -> Wallet | None:
    return (
        await db.execute(
            select(Wallet).where(Wallet.id == wallet_id, Wallet.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def wallet_balance(db: AsyncSession, wallet: Wallet) -> dict[str, Any]:
    amounts = await balances(
        db, [wallet.available_account_id, wallet.pending_account_id]
    )
    return {
        "wallet_id": wallet.id,
        "currency": wallet.currency,
        "available": amounts[wallet.available_account_id],
        "pending": amounts[wallet.pending_account_id],
    }


async def wallet_entries(
    db: AsyncSession, wallet: Wallet, *, limit: int, before: int | None
) -> dict[str, Any]:
    names = {
        wallet.available_account_id: "available",
        wallet.pending_account_id: "pending",
    }
    query = (
        select(Entry)
        .where(Entry.account_id.in_(list(names)))
        .order_by(Entry.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        query = query.where(Entry.id < before)
    rows = list((await db.execute(query)).scalars())
    page, more = rows[:limit], len(rows) > limit
    return {
        "wallet_id": wallet.id,
        "entries": [
            {
                "entry_id": e.id,
                "transfer_id": e.transfer_id,
                "account": names[e.account_id],
                "direction": e.direction,
                "amount": e.amount,
                "currency": e.currency,
                "created_at": e.created_at,
            }
            for e in page
        ],
        "next_before": page[-1].id if more and page else None,
    }


async def transfer_between_wallets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    from_wallet_id: uuid.UUID,
    to_wallet_id: uuid.UUID,
    amount: int,
) -> HandlerResult:
    """Business write for POST /v1/transfers: available -> available.

    Deterministic failures become stored rejections, so a retry with the same
    key gets the same answer.
    """
    source = await get_wallet(db, tenant_id=tenant_id, wallet_id=from_wallet_id)
    target = await get_wallet(db, tenant_id=tenant_id, wallet_id=to_wallet_id)
    if source is None or target is None:
        missing = from_wallet_id if source is None else to_wallet_id
        raise BusinessRejection(404, {"detail": f"Wallet {missing} not found"})
    if source.currency != target.currency:
        raise BusinessRejection(
            422,
            {
                "detail": "Wallets have different currencies "
                f"({source.currency} -> {target.currency}); FX is not supported"
            },
        )

    try:
        posted = await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference(
                posting_rules.WALLET_TRANSFER,
                "idempotency_key",
                idempotency_reference(tenant_id, idempotency_key),
            ),
            lines=posting_rules.wallet_transfer(
                amount, wallet_accounts(source), wallet_accounts(target)
            ),
        )
    except InsufficientFundsError as exc:
        raise BusinessRejection(
            422, {"detail": "Insufficient funds", "wallet_id": str(source.id)}
        ) from exc
    if not posted.created:
        # Same rule as payments: a swept key cannot post a second transfer.
        raise BusinessRejection(
            422,
            {
                "detail": "Idempotency-Key was already used for a transfer "
                "and has expired; use a new key"
            },
        )

    body = {
        "transfer_id": str(posted.transfer.id),
        "from_wallet_id": str(source.id),
        "to_wallet_id": str(target.id),
        "amount": amount,
        "currency": source.currency,
        "created_at": posted.transfer.created_at.isoformat(),
    }
    return HandlerResult(status_code=201, body=body, resource_id=posted.transfer.id)
