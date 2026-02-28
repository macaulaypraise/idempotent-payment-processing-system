from app.models.payment import PaymentStatus
from app.core.exceptions import InvalidStateTransitionError

VALID_TRANSITIONS = {
    PaymentStatus.PENDING: {PaymentStatus.PROCESSING},
    PaymentStatus.PROCESSING: {PaymentStatus.SETTLED, PaymentStatus.FAILED},
    PaymentStatus.SETTLED: {PaymentStatus.REFUNDED},
    PaymentStatus.FAILED: {PaymentStatus.PENDING},
    PaymentStatus.REFUNDED: set()
}

def validate_transition(current_status: PaymentStatus, new_status: PaymentStatus) -> None:
    allowed = VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvalidStateTransitionError(
            current_status=current_status.value,
            new_status=new_status.value
        )
