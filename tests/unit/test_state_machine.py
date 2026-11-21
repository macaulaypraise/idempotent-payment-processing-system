"""
Payment and refund state machines: the full transition matrix, and parity
between the Python maps and the SQL the migration installs as triggers.
"""

import importlib.util
import itertools
from pathlib import Path

import pytest

from app.core.exceptions import InvalidStateTransitionError
from app.models.payment import PaymentStatus
from app.models.refund import RefundStatus
from app.services.state_machine import (
    REFUND_TRANSITIONS,
    VALID_TRANSITIONS,
    validate_refund_transition,
    validate_transition,
)

ALLOWED_PAYMENT = {
    (PaymentStatus.PENDING, PaymentStatus.PROCESSING),
    (PaymentStatus.PROCESSING, PaymentStatus.CAPTURED),
    (PaymentStatus.PROCESSING, PaymentStatus.FAILED),
    (PaymentStatus.CAPTURED, PaymentStatus.SETTLED),
    (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED),
    (PaymentStatus.SETTLED, PaymentStatus.REFUNDED),
}
ALLOWED_REFUND = {
    (RefundStatus.PENDING, RefundStatus.PROCESSING),
    (RefundStatus.PROCESSING, RefundStatus.SUCCEEDED),
    (RefundStatus.PROCESSING, RefundStatus.FAILED),
}


@pytest.mark.parametrize(
    "current,new", list(itertools.product(PaymentStatus, PaymentStatus))
)
def test_payment_transition_matrix(current: PaymentStatus, new: PaymentStatus) -> None:
    if (current, new) in ALLOWED_PAYMENT:
        validate_transition(current, new)
    else:
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(current, new)


@pytest.mark.parametrize(
    "current,new", list(itertools.product(RefundStatus, RefundStatus))
)
def test_refund_transition_matrix(current: RefundStatus, new: RefundStatus) -> None:
    if (current, new) in ALLOWED_REFUND:
        validate_refund_transition(current, new)
    else:
        with pytest.raises(InvalidStateTransitionError):
            validate_refund_transition(current, new)


def test_failed_and_refunded_are_terminal() -> None:
    assert VALID_TRANSITIONS[PaymentStatus.FAILED] == set()
    assert VALID_TRANSITIONS[PaymentStatus.REFUNDED] == set()


def test_error_message_includes_statuses() -> None:
    with pytest.raises(InvalidStateTransitionError) as caught:
        validate_transition(PaymentStatus.FAILED, PaymentStatus.PENDING)
    assert "failed" in str(caught.value.detail)
    assert "pending" in str(caught.value.detail)


def _migration() -> object:
    path = next(Path("migrations/versions").glob("d4b9e2f7a315_*.py"))
    spec = importlib.util.spec_from_file_location("stage3_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_database_trigger_allows_exactly_the_python_transitions() -> None:
    migration = _migration()
    sql_payment = {
        (PaymentStatus[a], PaymentStatus[b])
        for a, b in migration.PAYMENT_TRANSITIONS  # type: ignore[attr-defined]
    }
    sql_refund = {
        (RefundStatus[a], RefundStatus[b])
        for a, b in migration.REFUND_TRANSITIONS  # type: ignore[attr-defined]
    }
    python_payment = {
        (a, b) for a, targets in VALID_TRANSITIONS.items() for b in targets
    }
    python_refund = {
        (a, b) for a, targets in REFUND_TRANSITIONS.items() for b in targets
    }
    assert sql_payment == python_payment == ALLOWED_PAYMENT
    assert sql_refund == python_refund == ALLOWED_REFUND
    assert set(migration.PAYMENT_STATUSES) == {s.name for s in PaymentStatus}  # type: ignore[attr-defined]
    assert set(migration.REFUND_STATUSES) == {s.name for s in RefundStatus}  # type: ignore[attr-defined]
