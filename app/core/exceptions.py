from fastapi import HTTPException


class IdempotencyConflictError(HTTPException):
    """The original request with this key is still in flight (lock_timeout)."""

    def __init__(self, message: str = "", retry_after_seconds: int = 1) -> None:
        super().__init__(
            status_code=409,
            detail="A request with this Idempotency-Key is still in progress",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class IdempotencyKeyMissingError(HTTPException):
    def __init__(self, detail: str = "Idempotency-Key header is required") -> None:
        super().__init__(status_code=400, detail=detail)


class IdempotencyKeyMismatchError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=422,
            detail="Idempotency-Key was already used with a different request",
        )


class UnauthorizedError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=401,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ForbiddenError(HTTPException):
    def __init__(self, detail: str = "Not allowed for this API key") -> None:
        super().__init__(status_code=403, detail=detail)


class InvalidStateTransitionError(HTTPException):
    def __init__(self, current_status: str, new_status: str) -> None:
        super().__init__(
            status_code=422,
            detail=f"Invalid transition: {current_status} → {new_status}",
        )


class PaymentNotFoundError(HTTPException):
    def __init__(self, payment_id: str) -> None:
        super().__init__(status_code=404, detail=f"Payment {payment_id} not found")


class BackpressureError(HTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(
            status_code=429,
            detail="System under heavy load. Please retry later.",
        )
