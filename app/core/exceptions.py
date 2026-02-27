from fastapi import HTTPException

class IdempotencyConflictError(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=409,
            detail="Duplicate request in progress"
        )

class InvalidStateTransitionError(HTTPException):
    def __init__(self, current_status: str, new_status: str):
        super().__init__(
            status_code=422,
            detail=f"Invalid transition: {current_status} → {new_status}"
        )

class PaymentNotFoundError(HTTPException):
    def __init__(self, payment_id: str):
        super().__init__(
            status_code=404,
            detail=f"Payment {payment_id} not found"
        )
