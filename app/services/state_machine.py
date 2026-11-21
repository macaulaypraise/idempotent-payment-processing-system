"""Payment and refund state machines (DESIGN 6.2).

Mirrored in the database by the payments_guard and refunds_guard triggers;
the Python check gives a clean error before a write is attempted.
"""

from app.core.exceptions import InvalidStateTransitionError
from app.models.payment import PaymentStatus
from app.models.refund import RefundStatus

VALID_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    PaymentStatus.PENDING: {PaymentStatus.PROCESSING},
    PaymentStatus.PROCESSING: {PaymentStatus.CAPTURED, PaymentStatus.FAILED},
    PaymentStatus.CAPTURED: {PaymentStatus.SETTLED, PaymentStatus.REFUNDED},
    PaymentStatus.SETTLED: {PaymentStatus.REFUNDED},
    # Terminal: re-driving a declined payment under the same provider key
    # would only replay the decline. A new attempt is a new payment.
    PaymentStatus.FAILED: set(),
    PaymentStatus.REFUNDED: set(),
}

REFUND_TRANSITIONS: dict[RefundStatus, set[RefundStatus]] = {
    RefundStatus.PENDING: {RefundStatus.PROCESSING},
    RefundStatus.PROCESSING: {RefundStatus.SUCCEEDED, RefundStatus.FAILED},
    RefundStatus.SUCCEEDED: set(),
    RefundStatus.FAILED: set(),
}

# Statuses in which the provider has taken the money (I4: exactly one capture)
CAPTURED_STATES = frozenset(
    {PaymentStatus.CAPTURED, PaymentStatus.SETTLED, PaymentStatus.REFUNDED}
)
REFUNDABLE_STATES = frozenset({PaymentStatus.CAPTURED, PaymentStatus.SETTLED})


def validate_transition(
    current_status: PaymentStatus, new_status: PaymentStatus
) -> None:
    if new_status not in VALID_TRANSITIONS.get(current_status, set()):
        raise InvalidStateTransitionError(
            current_status=current_status.value, new_status=new_status.value
        )


def validate_refund_transition(current: RefundStatus, new: RefundStatus) -> None:
    if new not in REFUND_TRANSITIONS.get(current, set()):
        raise InvalidStateTransitionError(
            current_status=current.value, new_status=new.value
        )
