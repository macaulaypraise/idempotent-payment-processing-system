"""Helpers to read PostgreSQL error details through SQLAlchemy's wrappers."""

from typing import Any

LOCK_NOT_AVAILABLE = "55P03"  # raised when lock_timeout expires
UNIQUE_VIOLATION = "23505"


def _candidates(exc: BaseException) -> list[Any]:
    orig = getattr(exc, "orig", None)
    found: list[Any] = [exc]
    if orig is not None:
        found.append(orig)
        cause = getattr(orig, "__cause__", None)
        if cause is not None:
            found.append(cause)
    return found


def pg_sqlstate(exc: BaseException) -> str | None:
    for candidate in _candidates(exc):
        for attr in ("sqlstate", "pgcode"):
            value = getattr(candidate, attr, None)
            if isinstance(value, str) and value:
                return value
    return None


def pg_constraint_name(exc: BaseException) -> str | None:
    for candidate in _candidates(exc):
        value = getattr(candidate, "constraint_name", None)
        if isinstance(value, str) and value:
            return value
    return None


def violates_constraint(exc: BaseException, name: str) -> bool:
    return pg_constraint_name(exc) == name or name in str(exc)
