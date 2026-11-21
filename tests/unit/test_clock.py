from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.clock import Clock, FakeClock, SystemClock, get_clock


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_get_clock_defaults_to_system_clock() -> None:
    assert isinstance(get_clock(), SystemClock)


def test_fake_clock_is_frozen_until_advanced() -> None:
    clock = FakeClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    assert clock.now() == clock.now()
    clock.advance(seconds=30)
    assert clock.now() == datetime(2026, 3, 1, 12, 0, 30, tzinfo=UTC)
    clock.advance(timedelta(minutes=1), seconds=1)
    assert clock.now() == datetime(2026, 3, 1, 12, 1, 31, tzinfo=UTC)


def test_fake_clock_normalises_to_utc() -> None:
    lagos = timezone(timedelta(hours=1))
    clock = FakeClock(datetime(2026, 3, 1, 13, 0, tzinfo=lagos))
    assert clock.now() == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert clock.now().tzinfo == UTC


def test_fake_clock_set_moves_forward_only() -> None:
    clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
    clock.set(datetime(2026, 3, 2, tzinfo=UTC))
    assert clock.now() == datetime(2026, 3, 2, tzinfo=UTC)
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 3, 1, tzinfo=UTC))


def test_fake_clock_rejects_backwards_advance() -> None:
    with pytest.raises(ValueError):
        FakeClock().advance(seconds=-1)


def test_fake_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 3, 1))
    with pytest.raises(ValueError):
        FakeClock().set(datetime(2026, 3, 1))


def test_both_clocks_satisfy_protocol() -> None:
    clocks: list[Clock] = [SystemClock(), FakeClock()]
    assert all(isinstance(c.now(), datetime) for c in clocks)
