"""What should be true, from the client's side alone (coding companion 7.2).

The model is rebuilt from a client history. It knows what the client asked
for and what the system acknowledged; it does not know what the provider
decided. Because processing is asynchronous, the model never predicts one
state. Once the system is quiet it asks the provider's ground truth what
happened, and derives what each row must then be:

  payment, charge succeeded at the provider -> SETTLED or REFUNDED
  payment, charge declined                  -> FAILED
  payment, no charge (retries exhausted)    -> PROCESSING (dead-lettered)
  refund, succeeded / failed / never sent   -> SUCCEEDED / FAILED / in flight

and every wallet's balance, to the minor unit:

  pending   = 0 (everything captured has been settled or refunded)
  available = charged payments - refunds not FAILED + transfers in - out
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from verification.history import History

PAYMENT_STATUSES = frozenset(
    {"PENDING", "PROCESSING", "CAPTURED", "SETTLED", "FAILED", "REFUNDED"}
)
REFUND_STATUSES = frozenset({"PENDING", "PROCESSING", "SUCCEEDED", "FAILED"})


@dataclass(frozen=True)
class PaymentIntent:
    key: str
    payment_id: uuid.UUID
    wallet_id: uuid.UUID
    amount: int


@dataclass(frozen=True)
class RefundIntent:
    key: str
    refund_id: uuid.UUID
    payment_id: uuid.UUID


@dataclass(frozen=True)
class TransferIntent:
    key: str
    from_wallet: uuid.UUID
    to_wallet: uuid.UUID
    amount: int


@dataclass
class Model:
    wallets: list[uuid.UUID] = field(default_factory=list)
    payments: dict[uuid.UUID, PaymentIntent] = field(default_factory=dict)
    refunds: dict[uuid.UUID, RefundIntent] = field(default_factory=dict)
    transfers: list[TransferIntent] = field(default_factory=list)

    @classmethod
    def from_history(cls, history: History) -> "Model":
        """Acknowledged client writes, each counted once however many times
        it was retried."""
        model = cls()
        seen: set[tuple[str, str]] = set()
        for op in history.client():
            if not op.acknowledged or not isinstance(op.body, dict):
                continue
            if op.key is not None:
                if (op.op, op.key) in seen:
                    continue
                seen.add((op.op, op.key))
            if op.op == "create_wallet":
                model.wallets.append(uuid.UUID(op.body["wallet_id"]))
            elif op.op == "create_payment":
                payment_id = uuid.UUID(op.body["payment_id"])
                model.payments[payment_id] = PaymentIntent(
                    op.key or "",
                    payment_id,
                    uuid.UUID(op.request["wallet_id"]),
                    int(op.request["amount"]),
                )
            elif op.op == "create_refund":
                refund_id = uuid.UUID(op.body["refund_id"])
                model.refunds[refund_id] = RefundIntent(
                    op.key or "", refund_id, uuid.UUID(op.request["payment_id"])
                )
            elif op.op == "create_transfer":
                model.transfers.append(
                    TransferIntent(
                        op.key or "",
                        uuid.UUID(op.request["from_wallet_id"]),
                        uuid.UUID(op.request["to_wallet_id"]),
                        int(op.request["amount"]),
                    )
                )
        return model

    # --- expectations once quiet --------------------------------------------------

    def quiescent_violations(
        self,
        payments: dict[uuid.UUID, dict[str, Any]],
        refunds: dict[uuid.UUID, dict[str, Any]],
        balances: dict[uuid.UUID, tuple[int, int]],
        ground_truth: dict[str, Any],
    ) -> list[str]:
        """Compare the quiet database with what the model and the provider's
        ground truth say must be true. `payments`/`refunds` map id to a row
        dict with at least status (and amount); `balances` maps wallet id to
        (pending, available)."""
        charges: dict[str, list[dict[str, Any]]] = {}
        for charge in ground_truth["charges"]:
            charges.setdefault(str(charge["merchant_reference"]), []).append(charge)
        provider_refunds = {
            str(r.get("merchant_reference")): r
            for r in ground_truth["refunds"]
            if r.get("merchant_reference")
        }
        violations: list[str] = []

        charged: set[uuid.UUID] = set()
        for payment_id, intent in self.payments.items():
            row = payments.get(payment_id)
            if row is None:
                violations.append(f"I10 acknowledged payment {payment_id} is missing")
                continue
            if row["amount"] != intent.amount:
                violations.append(
                    f"model payment {payment_id} amount {row['amount']} "
                    f"!= {intent.amount}"
                )
            found = charges.get(str(payment_id), [])
            outcome = found[0]["status"] if found else None
            expected = {
                "succeeded": {"SETTLED", "REFUNDED"},
                "declined": {"FAILED"},
                None: {"PROCESSING"},
            }[outcome]
            if row["status"] not in expected:
                violations.append(
                    f"model payment {payment_id} is {row['status']} but the provider "
                    f"says {outcome or 'no charge'}: expected one of {sorted(expected)}"
                )
            if outcome == "succeeded":
                charged.add(payment_id)

        held: dict[uuid.UUID, int] = {}
        for refund_id, refund_intent in self.refunds.items():
            row = refunds.get(refund_id)
            if row is None:
                violations.append(f"I10 acknowledged refund {refund_id} is missing")
                continue
            sent = provider_refunds.get(str(refund_id))
            expected_status = (
                {"SUCCEEDED"}
                if sent and sent["status"] == "succeeded"
                else {"FAILED"}
                if sent
                else {"PENDING", "PROCESSING"}
            )
            if row["status"] not in expected_status:
                violations.append(
                    f"model refund {refund_id} is {row['status']} "
                    "but the provider says "
                    f"{sent['status'] if sent else 'never sent'}"
                )
            if row["status"] != "FAILED":
                paid = refund_intent.payment_id
                held[paid] = held.get(paid, 0) + 1

        expected_available = {w: 0 for w in self.wallets}
        for payment_id in charged:
            intent = self.payments[payment_id]
            expected_available[intent.wallet_id] = (
                expected_available.get(intent.wallet_id, 0) + intent.amount
            )
        for payment_id, count in held.items():
            intent = self.payments[payment_id]
            expected_available[intent.wallet_id] -= intent.amount * count
        for transfer in self.transfers:
            expected_available[transfer.from_wallet] -= transfer.amount
            expected_available[transfer.to_wallet] += transfer.amount

        for wallet_id, available in expected_available.items():
            pending, actual = balances.get(wallet_id, (0, 0))
            if pending != 0:
                violations.append(f"model wallet {wallet_id} pending {pending} != 0")
            if actual != available:
                violations.append(
                    f"model wallet {wallet_id} available {actual} "
                    f"!= expected {available}"
                )
        return violations
