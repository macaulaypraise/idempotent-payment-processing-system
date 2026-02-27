from app.models.payment import PaymentStatus

VALID_TRANSITIONS = {
    PaymentStatus.PENDING: {PaymentStatus.PROCESSING},
    PaymentStatus.PROCESSING: {PaymentStatus.SETTLED, PaymentStatus.FAILED},
    PaymentStatus.SETTLED: {PaymentStatus.REFUNDED},
    PaymentStatus.FAILED: {PaymentStatus.PENDING},
    PaymentStatus.REFUNDED: set()  # terminal state, no transitions allowed
}

def validate_transition(current_status: PaymentStatus, new_status: PaymentStatus) -> None:
    allowed = VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {current_status.value} → {new_status.value}. "
            f"Allowed: {[s.value for s in allowed]}"
        )
