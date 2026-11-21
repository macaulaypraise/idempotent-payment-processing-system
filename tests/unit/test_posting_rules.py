"""
Exit gate: every posting rule in DESIGN 6.3, with its exact lines.
Pure functions, so this is the whole table in one place.
"""

import uuid
from collections.abc import Callable

import pytest

from app.models.ledger import AccountKind, Side
from app.models.payment import PaymentStatus
from app.services import posting_rules as rules
from app.services.ledger import LedgerError, Line
from app.services.posting_rules import SystemAccounts, WalletAccounts

W = WalletAccounts(pending=uuid.uuid4(), available=uuid.uuid4())
R = WalletAccounts(pending=uuid.uuid4(), available=uuid.uuid4())  # receiver
S = SystemAccounts(
    psp_clearing=uuid.uuid4(),
    bank=uuid.uuid4(),
    psp_fees=uuid.uuid4(),
    refunds_payable=uuid.uuid4(),
)
NAMES = {
    W.pending: "wallet_pending",
    W.available: "wallet_available",
    R.available: "receiver_available",
    S.psp_clearing: "psp_clearing",
    S.bank: "bank",
    S.psp_fees: "psp_fees",
    S.refunds_payable: "refunds_payable",
}


def _named(lines: list[Line]) -> list[tuple[str, str, int]]:
    return [(line.side.value, NAMES[line.account_id], line.amount) for line in lines]


TABLE: list[tuple[str, Callable[[], list[Line]], list[tuple[str, str, int]]]] = [
    (
        "capture confirmed",
        lambda: rules.capture(1000, W, S),
        [("debit", "psp_clearing", 1000), ("credit", "wallet_pending", 1000)],
    ),
    (
        "settlement line (charge) with fee",
        lambda: rules.settlement_charge(1000, 30, S),
        [
            ("debit", "bank", 970),
            ("debit", "psp_fees", 30),
            ("credit", "psp_clearing", 1000),
        ],
    ),
    (
        "settlement line (charge) without fee",
        lambda: rules.settlement_charge(1000, 0, S),
        [("debit", "bank", 1000), ("credit", "psp_clearing", 1000)],
    ),
    (
        "release after settlement",
        lambda: rules.release(1000, W),
        [("debit", "wallet_pending", 1000), ("credit", "wallet_available", 1000)],
    ),
    (
        "wallet-to-wallet transfer",
        lambda: rules.wallet_transfer(250, W, R),
        [("debit", "wallet_available", 250), ("credit", "receiver_available", 250)],
    ),
    (
        "refund hold, payment CAPTURED",
        lambda: rules.refund_hold(1000, PaymentStatus.CAPTURED, W, S),
        [("debit", "wallet_pending", 1000), ("credit", "refunds_payable", 1000)],
    ),
    (
        "refund hold, payment SETTLED",
        lambda: rules.refund_hold(1000, PaymentStatus.SETTLED, W, S),
        [("debit", "wallet_available", 1000), ("credit", "refunds_payable", 1000)],
    ),
    (
        "refund confirmed",
        lambda: rules.refund_confirm(1000, S),
        [("debit", "refunds_payable", 1000), ("credit", "psp_clearing", 1000)],
    ),
    (
        "refund failed, payment still CAPTURED",
        lambda: rules.refund_fail(1000, PaymentStatus.CAPTURED, W, S),
        [("debit", "refunds_payable", 1000), ("credit", "wallet_pending", 1000)],
    ),
    (
        "refund failed, payment SETTLED meanwhile",
        lambda: rules.refund_fail(1000, PaymentStatus.SETTLED, W, S),
        [("debit", "refunds_payable", 1000), ("credit", "wallet_available", 1000)],
    ),
    (
        "settlement line (refund)",
        lambda: rules.settlement_refund(1000, S),
        [("debit", "psp_clearing", 1000), ("credit", "bank", 1000)],
    ),
]


@pytest.mark.parametrize("name,rule,expected", TABLE, ids=[t[0] for t in TABLE])
def test_posting_rule(
    name: str, rule: Callable[[], list[Line]], expected: list[tuple[str, str, int]]
) -> None:
    lines = rule()
    assert _named(lines) == expected
    debits = sum(line.amount for line in lines if line.side is Side.DEBIT)
    credits = sum(line.amount for line in lines if line.side is Side.CREDIT)
    assert debits == credits


@pytest.mark.parametrize("fee", [-1, 1000, 1001])
def test_settlement_fee_must_be_below_gross(fee: int) -> None:
    with pytest.raises(LedgerError):
        rules.settlement_charge(1000, fee, S)


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.PENDING,
        PaymentStatus.PROCESSING,
        PaymentStatus.FAILED,
        PaymentStatus.REFUNDED,
    ],
)
def test_no_hold_source_for_unrefundable_statuses(status: PaymentStatus) -> None:
    with pytest.raises(LedgerError):
        rules.hold_source(status)


def test_hold_source_follows_settlement() -> None:
    assert rules.hold_source(PaymentStatus.CAPTURED) is AccountKind.WALLET_PENDING
    assert rules.hold_source(PaymentStatus.SETTLED) is AccountKind.WALLET_AVAILABLE


def test_system_accounts_from_kinds() -> None:
    ids = {kind: uuid.uuid4() for kind in AccountKind}
    system = SystemAccounts.from_kinds(ids)
    assert system.psp_clearing == ids[AccountKind.PSP_CLEARING]
    assert system.refunds_payable == ids[AccountKind.REFUNDS_PAYABLE]
