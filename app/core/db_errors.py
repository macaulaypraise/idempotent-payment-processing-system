"""Helpers to read PostgreSQL error details through SQLAlchemy's wrappers."""

import socket
from typing import Any

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

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


# Network failures (a reset, a refusal, a timeout, an unresolvable host);
# other OSErrors, a missing file say, are bugs and stay 500s
_NETWORK = (ConnectionError, TimeoutError, socket.gaierror)

# Driver errors that mean "the connection is gone", not "the query is wrong"
_CONNECTION_ERRORS = frozenset(
    {
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "InterfaceError",
        "InternalClientError",
        "PostgresConnectionError",
        "CannotConnectNowError",
        "TooManyConnectionsError",
    }
)


def is_dependency_failure(exc: BaseException) -> bool:
    """True when a request failed because Postgres (or another dependency)
    could not be reached or dropped the connection, as opposed to a bug or
    a constraint. The outcome of such a request is unknown to the client:
    it must retry with the same idempotency key, so the API answers 503
    with Retry-After rather than 500.

    Seen under fault injection: ConnectionResetError while connecting, and
    DBAPIError wrapping asyncpg's ConnectionDoesNotExistError mid-request."""
    if isinstance(exc, _NETWORK):
        return True
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated or isinstance(
            exc, OperationalError | InterfaceError
        ):
            return True
        return any(
            type(c).__name__ in _CONNECTION_ERRORS or isinstance(c, _NETWORK)
            for c in _candidates(exc)[1:]
        )
    return False
