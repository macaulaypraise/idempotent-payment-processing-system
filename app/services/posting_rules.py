"""Posting rules: one pure function per row of DESIGN section 6.3.

No database access: each takes amounts and account ids and returns ledger
lines, which makes them trivial to test exhaustively. Services fetch the
accounts, call the rule, and hand the lines to post_transfer.

Event                      Debit                          Credit
-------------------------  -----------------------------  -----------------------
Capture confirmed          psp_clearing                   wallet_pending
Settlement line (charge)   bank (net), psp_fees (fee)     psp_clearing (gross)
Release after settlement   wallet_pending                 wallet_available
Wallet-to-wallet transfer  sender wallet_available        receiver w. available
Refund requested (hold)    wallet_pending if CAPTURED,    refunds_payable
                           else wallet_available
Refund confirmed           refunds_payable                psp_clearing
Refund failed (hold back)  refunds_payable                wallet_pending if
                                                          CAPTURED now, else
                                                          wallet_available
Settlement line (refund)   psp_clearing                   bank
"""

import uuid
from dataclasses import dataclass

from app.models.ledger import AccountKind
from app.models.payment import PaymentStatus
from app.services.ledger import LedgerError, Line, credit, debit

# Transfer kinds, used in each transfer's unique reference.
CAPTURE = "capture"
SETTLEMENT_CHARGE = "settlement_charge"
RELEASE = "release"
WALLET_TRANSFER = "wallet_transfer"
REFUND_HOLD = "refund_hold"
REFUND_CONFIRM = "refund_confirm"
REFUND_RELEASE = "refund_release"
SETTLEMENT_REFUND = "settlement_refund"


@dataclass(frozen=True)
class WalletAccounts:
    pending: uuid.UUID
    available: uuid.UUID


@dataclass(frozen=True)
class SystemAccounts:
    psp_clearing: uuid.UUID
    bank: uuid.UUID
    psp_fees: uuid.UUID
    refunds_payable: uuid.UUID

    @classmethod
    def from_kinds(cls, ids: dict[AccountKind, uuid.UUID]) -> "SystemAccounts":
        return cls(
            psp_clearing=ids[AccountKind.PSP_CLEARING],
            bank=ids[AccountKind.BANK],
            psp_fees=ids[AccountKind.PSP_FEES],
            refunds_payable=ids[AccountKind.REFUNDS_PAYABLE],
        )


def capture(amount: int, wallet: WalletAccounts, system: SystemAccounts) -> list[Line]:
    return [debit(system.psp_clearing, amount), credit(wallet.pending, amount)]


def settlement_charge(gross: int, fee: int, system: SystemAccounts) -> list[Line]:
    if fee < 0 or fee >= gross:
        raise LedgerError(f"fee must be in [0, gross): fee={fee}, gross={gross}")
    lines = [debit(system.bank, gross - fee), credit(system.psp_clearing, gross)]
    if fee:
        lines.insert(1, debit(system.psp_fees, fee))
    return lines


def release(amount: int, wallet: WalletAccounts) -> list[Line]:
    return [debit(wallet.pending, amount), credit(wallet.available, amount)]


def wallet_transfer(
    amount: int, sender: WalletAccounts, receiver: WalletAccounts
) -> list[Line]:
    return [debit(sender.available, amount), credit(receiver.available, amount)]


def hold_source(payment_status: PaymentStatus) -> AccountKind:
    """Where a refund's money sits: still pending until settlement releases it."""
    if payment_status is PaymentStatus.CAPTURED:
        return AccountKind.WALLET_PENDING
    if payment_status is PaymentStatus.SETTLED:
        return AccountKind.WALLET_AVAILABLE
    raise LedgerError(f"no refund hold source for a {payment_status.name} payment")


def _wallet_account(kind: AccountKind, wallet: WalletAccounts) -> uuid.UUID:
    return wallet.pending if kind is AccountKind.WALLET_PENDING else wallet.available


def refund_hold(
    amount: int,
    payment_status: PaymentStatus,
    wallet: WalletAccounts,
    system: SystemAccounts,
) -> list[Line]:
    source = _wallet_account(hold_source(payment_status), wallet)
    return [debit(source, amount), credit(system.refunds_payable, amount)]


def refund_confirm(amount: int, system: SystemAccounts) -> list[Line]:
    return [debit(system.refunds_payable, amount), credit(system.psp_clearing, amount)]


def refund_fail(
    amount: int,
    payment_status_now: PaymentStatus,
    wallet: WalletAccounts,
    system: SystemAccounts,
) -> list[Line]:
    """Return the hold to where the money belongs *now*: if the payment
    settled while the refund was in flight, that is the available account."""
    target = _wallet_account(hold_source(payment_status_now), wallet)
    return [debit(system.refunds_payable, amount), credit(target, amount)]


def settlement_refund(amount: int, system: SystemAccounts) -> list[Line]:
    return [debit(system.psp_clearing, amount), credit(system.bank, amount)]
