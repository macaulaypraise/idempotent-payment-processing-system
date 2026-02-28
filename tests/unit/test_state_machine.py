import pytest
from app.models.payment import PaymentStatus
from app.services.state_machine import validate_transition
from app.core.exceptions import InvalidStateTransitionError


def test_valid_transition_pending_to_processing():
    """PENDING → PROCESSING is the first valid transition."""
    validate_transition(PaymentStatus.PENDING, PaymentStatus.PROCESSING)  # should not raise


def test_valid_transition_processing_to_settled():
    """PROCESSING → SETTLED is the success path."""
    validate_transition(PaymentStatus.PROCESSING, PaymentStatus.SETTLED)  # should not raise


def test_valid_transition_processing_to_failed():
    """PROCESSING → FAILED is the failure path."""
    validate_transition(PaymentStatus.PROCESSING, PaymentStatus.FAILED)  # should not raise


def test_valid_transition_settled_to_refunded():
    """SETTLED → REFUNDED is the refund path."""
    validate_transition(PaymentStatus.SETTLED, PaymentStatus.REFUNDED)  # should not raise


def test_invalid_transition_pending_to_refunded():
    """PENDING → REFUNDED is invalid — must go through PROCESSING first."""
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.REFUNDED)


def test_invalid_transition_pending_to_settled():
    """PENDING → SETTLED skips PROCESSING — invalid."""
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.SETTLED)


def test_invalid_transition_refunded_is_terminal():
    """REFUNDED is a terminal state — no transitions allowed out of it."""
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.REFUNDED, PaymentStatus.PENDING)


def test_invalid_transition_failed_to_settled():
    """FAILED → SETTLED is invalid — failed payments cannot be directly settled."""
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.FAILED, PaymentStatus.SETTLED)
