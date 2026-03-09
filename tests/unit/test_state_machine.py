"""
Unit tests for app/services/state_machine.py
Target: 100% coverage — pure logic, no I/O
"""

import pytest

from app.core.exceptions import InvalidStateTransitionError
from app.models.payment import PaymentStatus
from app.services.state_machine import validate_transition

# Valid transitions — must not raise


def test_pending_to_processing() -> None:
    validate_transition(PaymentStatus.PENDING, PaymentStatus.PROCESSING)


def test_processing_to_settled() -> None:
    validate_transition(PaymentStatus.PROCESSING, PaymentStatus.SETTLED)


def test_processing_to_failed() -> None:
    validate_transition(PaymentStatus.PROCESSING, PaymentStatus.FAILED)


def test_settled_to_refunded() -> None:
    validate_transition(PaymentStatus.SETTLED, PaymentStatus.REFUNDED)


def test_failed_to_pending() -> None:
    validate_transition(PaymentStatus.FAILED, PaymentStatus.PENDING)


# Invalid transitions — must raise InvalidStateTransitionError


def test_pending_to_settled_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.SETTLED)


def test_pending_to_refunded_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.REFUNDED)


def test_pending_to_failed_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.PENDING, PaymentStatus.FAILED)


def test_settled_to_processing_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.SETTLED, PaymentStatus.PROCESSING)


def test_settled_to_pending_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.SETTLED, PaymentStatus.PENDING)


def test_settled_to_failed_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.SETTLED, PaymentStatus.FAILED)


def test_refunded_to_any_raises() -> None:
    for target in PaymentStatus:
        if target != PaymentStatus.REFUNDED:
            with pytest.raises(InvalidStateTransitionError):
                validate_transition(PaymentStatus.REFUNDED, target)


def test_failed_to_settled_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.FAILED, PaymentStatus.SETTLED)


def test_failed_to_refunded_raises() -> None:
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(PaymentStatus.FAILED, PaymentStatus.REFUNDED)


# Error carries current and requested status


def test_error_message_includes_statuses() -> None:
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        validate_transition(PaymentStatus.PENDING, PaymentStatus.REFUNDED)
    msg = str(exc_info.value).lower()
    assert "pending" in msg
    assert "refunded" in msg


# Enum completeness


def test_all_expected_statuses_exist() -> None:
    statuses = {s.value for s in PaymentStatus}
    assert "pending" in statuses
    assert "processing" in statuses
    assert "settled" in statuses
    assert "failed" in statuses
    assert "refunded" in statuses
