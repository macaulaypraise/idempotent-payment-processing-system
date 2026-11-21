"""
Unit tests for app/core/exceptions.py
Verifies each exception carries the right HTTP status and message.
"""

from app.core.exceptions import (
    BackpressureError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PaymentNotFoundError,
)


def test_invalid_state_transition_is_exception() -> None:
    exc = InvalidStateTransitionError("PENDING", "REFUNDED")
    assert isinstance(exc, Exception)
    assert "PENDING" in str(exc)
    assert "REFUNDED" in str(exc)


def test_idempotency_conflict_is_exception() -> None:
    exc = IdempotencyConflictError("key-1")
    assert isinstance(exc, Exception)


def test_payment_not_found_is_exception() -> None:
    exc = PaymentNotFoundError("pay-123")
    assert isinstance(exc, Exception)
    assert "pay-123" in str(exc)


def test_backpressure_error_is_exception() -> None:
    exc = BackpressureError("queue full")
    assert isinstance(exc, Exception)


def test_each_exception_has_distinct_type() -> None:
    types = {
        InvalidStateTransitionError,
        IdempotencyConflictError,
        PaymentNotFoundError,
        BackpressureError,
    }
    assert len(types) == 4


def test_new_idempotency_errors_have_contract_status_codes() -> None:
    from app.core.exceptions import (
        IdempotencyKeyMismatchError,
        IdempotencyKeyMissingError,
        UnauthorizedError,
    )

    assert IdempotencyKeyMissingError().status_code == 400
    assert IdempotencyKeyMismatchError().status_code == 422
    assert UnauthorizedError().status_code == 401
    assert UnauthorizedError().headers == {"WWW-Authenticate": "Bearer"}
    conflict = IdempotencyConflictError()
    assert conflict.status_code == 409
    assert conflict.headers == {"Retry-After": "1"}
