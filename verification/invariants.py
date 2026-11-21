"""System invariants as SQL checks (DESIGN section 3).

Each check returns a list of human-readable violations; an empty list means
the invariant holds. Checks take an optional tenant to scope the query, so
tests that share a database can check only their own data. I2 is global by
nature; with a tenant it checks that tenant's slice of the ledger.

Implemented: I1, I2, I5, I6 (Stage 2); I4 for payments and refunds
(Stage 3). I3, I7-I10 arrive with the stages that create the data they check.
"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Check = Callable[[AsyncSession, uuid.UUID | None], Awaitable[list[str]]]

_SIGNED = "CASE WHEN e.direction = 'debit' THEN e.amount ELSE -e.amount END"


def _tenant_filter(alias: str, tenant_id: uuid.UUID | None) -> str:
    return f"AND {alias}.tenant_id = :tenant_id" if tenant_id else ""


async def i1_transfers_balance(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """I1: every transfer has >= 2 entries and nets to zero per currency."""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT t.id, e.currency, count(e.id) AS lines,
                       coalesce(sum({_SIGNED}), 0) AS net
                FROM transfers t LEFT JOIN entries e ON e.transfer_id = t.id
                WHERE true {_tenant_filter("t", tenant_id)}
                GROUP BY t.id, e.currency
                HAVING count(e.id) < 2 OR coalesce(sum({_SIGNED}), 0) <> 0
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).all()
    return [
        f"I1 transfer {r.id} ({r.currency}): {r.lines} lines, net {r.net}" for r in rows
    ]


async def i2_ledger_balances(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """I2: across the ledger, total debits equal total credits per currency."""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT e.currency, sum({_SIGNED}) AS net
                FROM entries e JOIN accounts a ON a.id = e.account_id
                WHERE true {_tenant_filter("a", tenant_id)}
                GROUP BY e.currency
                HAVING sum({_SIGNED}) <> 0
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).all()
    return [f"I2 ledger out of balance in {r.currency}: net {r.net}" for r in rows]


async def i5_account_totals_match_entries(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """I5: each account's cached totals equal the sums of its entries."""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT a.id, a.debits_posted, a.credits_posted,
                       coalesce(s.debits, 0) AS debits,
                       coalesce(s.credits, 0) AS credits
                FROM accounts a
                LEFT JOIN (
                    SELECT e.account_id,
                           sum(e.amount) FILTER (WHERE e.direction = 'debit')
                               AS debits,
                           sum(e.amount) FILTER (WHERE e.direction = 'credit')
                               AS credits
                    FROM entries e GROUP BY e.account_id
                ) s ON s.account_id = a.id
                WHERE (a.debits_posted <> coalesce(s.debits, 0)
                       OR a.credits_posted <> coalesce(s.credits, 0))
                      {_tenant_filter("a", tenant_id)}
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).all()
    return [
        f"I5 account {r.id}: cached {r.debits_posted}/{r.credits_posted}, "
        f"entries {r.debits}/{r.credits}"
        for r in rows
    ]


async def i6_guarded_accounts_not_negative(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """I6: no guarded account has debits greater than credits."""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT a.id, a.kind, a.debits_posted, a.credits_posted
                FROM accounts a
                WHERE a.guarded AND a.debits_posted > a.credits_posted
                      {_tenant_filter("a", tenant_id)}
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).all()
    return [
        f"I6 {r.kind} account {r.id}: debits {r.debits_posted} > credits "
        f"{r.credits_posted}"
        for r in rows
    ]


async def i4_payment_captures(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """I4: CAPTURED, SETTLED and REFUNDED payments have exactly one capture
    transfer; PENDING, PROCESSING and FAILED have none. Pre-ledger (v1)
    payments without a wallet are out of scope."""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT p.id, p.status, count(t.id) AS captures
                FROM payments p
                LEFT JOIN transfers t
                  ON t.tenant_id = p.tenant_id AND t.kind = 'capture'
                 AND t.reference_type = 'payment' AND t.reference_id = p.id
                WHERE p.wallet_id IS NOT NULL {_tenant_filter("p", tenant_id)}
                GROUP BY p.id, p.status
                HAVING count(t.id) <> CASE
                    WHEN p.status IN ('CAPTURED', 'SETTLED', 'REFUNDED') THEN 1
                    ELSE 0 END
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).all()
    return [f"I4 payment {r.id} is {r.status} with {r.captures} captures" for r in rows]


async def i4_refund_postings(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    """I4 (refunds): every refund has exactly one hold; SUCCEEDED has one
    confirm and no release; FAILED has one release and no confirm; in-flight
    refunds have neither. A SUCCEEDED refund's payment is REFUNDED."""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT r.id, r.status, p.status AS payment_status,
                       count(*) FILTER (WHERE t.kind = 'refund_hold') AS holds,
                       count(*) FILTER (WHERE t.kind = 'refund_confirm') AS confirms,
                       count(*) FILTER (WHERE t.kind = 'refund_release') AS releases
                FROM refunds r
                JOIN payments p ON p.id = r.payment_id
                LEFT JOIN transfers t
                  ON t.tenant_id = r.tenant_id AND t.reference_type = 'refund'
                 AND t.reference_id = r.id
                WHERE true {_tenant_filter("r", tenant_id)}
                GROUP BY r.id, r.status, p.status
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).all()
    expected = {
        "PENDING": (1, 0, 0),
        "PROCESSING": (1, 0, 0),
        "SUCCEEDED": (1, 1, 0),
        "FAILED": (1, 0, 1),
    }
    violations = [
        f"I4 refund {r.id} is {r.status} with holds/confirms/releases "
        f"{r.holds}/{r.confirms}/{r.releases}"
        for r in rows
        if (r.holds, r.confirms, r.releases) != expected[r.status]
    ]
    violations.extend(
        f"I4 refund {r.id} SUCCEEDED but payment is {r.payment_status}"
        for r in rows
        if r.status == "SUCCEEDED" and r.payment_status != "REFUNDED"
    )
    return violations


LEDGER_INVARIANTS: tuple[Check, ...] = (
    i1_transfers_balance,
    i2_ledger_balances,
    i5_account_totals_match_entries,
    i6_guarded_accounts_not_negative,
    i4_payment_captures,
    i4_refund_postings,
)


async def check_ledger(
    db: AsyncSession, tenant_id: uuid.UUID | None = None
) -> list[str]:
    violations: list[str] = []
    for check in LEDGER_INVARIANTS:
        violations.extend(await check(db, tenant_id))
    return violations
