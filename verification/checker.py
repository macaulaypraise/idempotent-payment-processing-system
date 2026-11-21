"""The offline checker (coding companion, Stage 7.5).

Inputs: a client history (JSONL), the database, and the provider's ground
truth. Output: every violated invariant, or nothing.

  safety        I1 I2 I3 I5 I6   hold at every instant
  completeness  I4 I7            hold once the system is quiet and reconciled
  replays       I9               same key and body -> identical response;
                                 same key, other body -> 422
  model         I10 + balances   acknowledged writes exist, and every row and
                                 wallet balance is what the history and the
                                 provider's ground truth imply

    python -m verification.checker --history run.jsonl --ground-truth truth.json

The in-process state machine calls the same functions after every example.
"""

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from verification import invariants
from verification.history import History
from verification.model import Model


async def safety(db: AsyncSession, ground_truth: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for check in (
        invariants.i1_transfers_balance,
        invariants.i2_ledger_balances,
        invariants.i3_one_payment_per_key,
        invariants.i5_account_totals_match_entries,
        invariants.i6_guarded_accounts_not_negative,
    ):
        violations.extend(await check(db, None))
    violations.extend(invariants.i3_one_charge_per_payment(ground_truth["charges"]))
    return violations


async def completeness(db: AsyncSession) -> list[str]:
    violations = await invariants.i4_payment_captures(db)
    violations += await invariants.i4_refund_postings(db)
    violations += await invariants.i7_clearing_accounts_settled(db)
    return violations


def replays(history: History) -> list[str]:
    """I9: every answered request under one key gets the first answer again,
    or 422 if its body differs from the first answered body."""
    first: dict[tuple[str, str], Any] = {}
    violations: list[str] = []
    for op in history.client():
        if op.key is None or op.outcome == "unknown":
            continue
        slot = (op.op, op.key)
        if slot not in first:
            first[slot] = op
            continue
        original = first[slot]
        if op.request == original.request:
            if (op.status, op.body) != (original.status, original.body):
                violations.append(
                    f"I9 {op.op} key {op.key!r}: replay #{op.seq} answered "
                    f"{op.status} {op.body!r}, first answer #{original.seq} was "
                    f"{original.status} {original.body!r}"
                )
        elif op.status != 422:
            violations.append(
                f"I9 {op.op} key {op.key!r}: different body at #{op.seq} answered "
                f"{op.status}, expected 422"
            )
    return violations


async def model_violations(
    db: AsyncSession, history: History, ground_truth: dict[str, Any]
) -> list[str]:
    from app.models.payment import Payment
    from app.models.refund import Refund
    from app.models.wallet import Wallet
    from app.services.ledger import balances

    model = Model.from_history(history)
    payments = {
        p.id: {"status": p.status.name, "amount": p.amount}
        for p in (
            await db.execute(
                select(Payment).where(Payment.id.in_(list(model.payments)))
            )
        ).scalars()
    }
    refunds = {
        r.id: {"status": r.status.name, "amount": r.amount}
        for r in (
            await db.execute(select(Refund).where(Refund.id.in_(list(model.refunds))))
        ).scalars()
    }
    wallets = (
        (await db.execute(select(Wallet).where(Wallet.id.in_(model.wallets))))
        .scalars()
        .all()
    )
    amounts = await balances(
        db, [a for w in wallets for a in (w.pending_account_id, w.available_account_id)]
    )
    wallet_balances = {
        w.id: (amounts[w.pending_account_id], amounts[w.available_account_id])
        for w in wallets
    }
    return model.quiescent_violations(payments, refunds, wallet_balances, ground_truth)


async def unexplained_reconciliation_items(db: AsyncSession) -> list[str]:
    """No settlement faults are injected, so every item must be explained."""
    rows = (
        await db.execute(
            text(
                "SELECT i.classification, i.payment_id, i.detail FROM recon_items i "
                "JOIN recon_runs r ON r.id = i.run_id AND r.status = 'completed' "
                "WHERE i.classification NOT IN ('matched', 'healed')"
            )
        )
    ).all()
    return [
        f"recon {r.classification} for payment {r.payment_id}: {r.detail}" for r in rows
    ]


async def check_all(
    db: AsyncSession,
    history: History,
    ground_truth: dict[str, Any],
    *,
    quiet: bool = True,
    settlement_faults: bool = False,
) -> list[str]:
    violations = await safety(db, ground_truth) + replays(history)
    if quiet:
        violations += await completeness(db)
        violations += await model_violations(db, history, ground_truth)
        if not settlement_faults:
            violations += await unexplained_reconciliation_items(db)
    return violations


async def _main(args: argparse.Namespace) -> int:  # pragma: no cover - CLI wiring
    from app.config import get_settings

    history = History.load(args.history)
    with open(args.ground_truth) as source:
        ground_truth = json.load(source)
    engine = create_async_engine(args.database_url or get_settings().DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        violations = await check_all(
            db,
            history,
            ground_truth,
            quiet=not args.not_quiet,
            settlement_faults=args.settlement_faults,
        )
    await engine.dispose()
    client_ops = sum(1 for _ in history.client())
    print(json.dumps({"operations": client_ops, "violations": violations}, indent=2))
    return 1 if violations else 0


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(prog="python -m verification.checker")
    parser.add_argument("--history", required=True)
    parser.add_argument(
        "--ground-truth", required=True, help="JSON from GET /ground-truth"
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--not-quiet", action="store_true", help="safety checks only")
    parser.add_argument("--settlement-faults", action="store_true")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":  # pragma: no cover
    _ = uuid  # keep import for type users of this module
    main()
