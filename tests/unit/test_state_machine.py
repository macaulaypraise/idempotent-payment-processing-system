import pytest
from app.services.state_machine import validate_transition
from app.models.payment import PaymentStatus

def test_valid_transition_pending_to_processing():
    validate_transition(PaymentStatus.PENDING, PaymentStatus.PROCESSING)

def test_invalid_transition_pending_to_refunded():
    with pytest.raises(ValueError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.REFUNDED)

def test_invalid_transition_refunded_is_terminal():
    with pytest.raises(ValueError):
        validate_transition(PaymentStatus.REFUNDED, PaymentStatus.PENDING)
