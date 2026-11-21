"""Single source of "now" for the application.

Every backoff, lease, expiry and tolerance check takes a Clock by injection
instead of calling datetime.now() directly. Production uses SystemClock; tests
and the Stage 7 verification harness use FakeClock to control time without
sleeping.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock: time only moves when advance() or set() is called."""

    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | None = None, *, seconds: float = 0) -> None:
        step = (delta or timedelta()) + timedelta(seconds=seconds)
        if step < timedelta():
            raise ValueError("FakeClock cannot move backwards")
        self._now += step

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        moment = moment.astimezone(UTC)
        if moment < self._now:
            raise ValueError("FakeClock cannot move backwards")
        self._now = moment


_default_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """FastAPI dependency / default provider. Override in tests."""
    return _default_clock
