"""app.core.shutdown: the stop event the worker loops wait on."""

import asyncio
import os
import signal
import time

from app.core.shutdown import SIGNALS, sleep_or_stop, stop_on_signals


async def test_sigterm_sets_the_stop_event_and_wakes_a_sleeping_loop() -> None:
    loop = asyncio.get_running_loop()
    stop = stop_on_signals("test")
    try:
        loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        started = time.monotonic()
        assert await sleep_or_stop(30, stop) is True
        assert time.monotonic() - started < 5
        assert stop.is_set()
    finally:
        for signum in SIGNALS:
            loop.remove_signal_handler(signum)


async def test_sleep_or_stop_sleeps_when_nobody_stops_it() -> None:
    stop = asyncio.Event()
    started = time.monotonic()
    assert await sleep_or_stop(0.05, stop) is False
    assert time.monotonic() - started >= 0.04
