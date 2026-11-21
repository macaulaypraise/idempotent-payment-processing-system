"""Crash points for fault-injection tests.

    failpoint("charger.after_provider_call")

does nothing unless that name is armed. Armed with mode "exit", the process
dies on the spot (os._exit, no cleanup, no commit), exactly like a crash.
Mode "raise" throws SimulatedCrash instead, which in-process tests use: it
derives from BaseException so ordinary `except Exception` handlers cannot
swallow it.

Arm through the FAILPOINTS setting ("name=exit,other=raise") or arm() in
tests. Failpoints never fire when APP_ENV is "production".
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

from app.config import get_settings

logger = structlog.get_logger()

EXIT_CODE = 137  # what a SIGKILLed process reports


class SimulatedCrash(BaseException):  # noqa: N818 - it is a crash, not an error
    pass


_armed: dict[str, str] = {}


def _from_settings() -> dict[str, str]:
    armed: dict[str, str] = {}
    for item in get_settings().FAILPOINTS.split(","):
        name, _, mode = item.strip().partition("=")
        if name:
            armed[name] = mode or "exit"
    return armed


def failpoint(name: str) -> None:
    settings = get_settings()
    if settings.APP_ENV == "production":
        return
    mode = _armed.get(name) or _from_settings().get(name)
    if mode is None:
        return
    logger.warning("failpoint_triggered", failpoint=name, mode=mode)
    if mode == "raise":
        raise SimulatedCrash(name)
    os._exit(EXIT_CODE)


def arm(name: str, mode: str = "raise") -> None:
    if mode not in ("raise", "exit"):
        raise ValueError(f"unknown failpoint mode {mode!r}")
    _armed[name] = mode


def disarm(name: str) -> None:
    _armed.pop(name, None)


@contextmanager
def armed(name: str, mode: str = "raise") -> Iterator[None]:
    arm(name, mode)
    try:
        yield
    finally:
        disarm(name)
