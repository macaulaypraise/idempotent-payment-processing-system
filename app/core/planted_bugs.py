"""Deliberate bugs the verification harness must catch: its self-test.

A harness that cannot find a planted bug proves nothing. Each bug here is a
real mistake from the coding companion's list, behind a switch that is off
unless armed in a test (`plant`) or through HARNESS_PLANTED_BUGS, and that
can never turn on when APP_ENV is production.

  fresh_provider_key  the charger sends a new idempotency key on every retry,
                      so a retry after a lost response charges again (I3).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from app.config import get_settings

KNOWN = frozenset({"fresh_provider_key"})
_planted: set[str] = set()


def planted(name: str) -> bool:
    settings = get_settings()
    if settings.APP_ENV == "production":
        return False
    configured = {b.strip() for b in settings.HARNESS_PLANTED_BUGS.split(",")}
    return name in _planted or name in configured


@contextmanager
def plant(name: str) -> Iterator[None]:
    if name not in KNOWN:
        raise ValueError(f"unknown planted bug {name!r}; known: {sorted(KNOWN)}")
    _planted.add(name)
    try:
        yield
    finally:
        _planted.discard(name)
