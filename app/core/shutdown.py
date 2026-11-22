"""Graceful shutdown for the worker processes.

Each worker runs as PID 1 in its container, and the kernel delivers no
signal to PID 1 that it has no handler for. Without a handler, SIGTERM from
`docker stop` (or a deploy) was ignored: every stop waited the whole grace
period and ended in SIGKILL (found by the fault layer, backlog S7-7).

    stop = stop_on_signals()
    while not stop.is_set():
        await do_one_round()
        await sleep_or_stop(interval, stop)

A worker that is stopped finishes the item in hand, then leaves: the charger
leaves its Kafka group, the relay releases its leader lock. A SIGKILL is
still safe (every worker is crash-safe by design); this only makes ordinary
stops quick and clean.
"""

import asyncio
import contextlib
import signal

import structlog

logger = structlog.get_logger()

SIGNALS = (signal.SIGTERM, signal.SIGINT)


def stop_on_signals(name: str = "worker") -> asyncio.Event:
    """An event set by SIGTERM or SIGINT. Call inside the running loop."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signum: signal.Signals) -> None:
        if not stop.is_set():
            logger.info("worker_stopping", worker=name, signal=signum.name)
        stop.set()

    for signum in SIGNALS:
        loop.add_signal_handler(signum, request_stop, signum)
    return stop


async def sleep_or_stop(seconds: float, stop: asyncio.Event) -> bool:
    """Sleep, waking early on stop; True if stopping."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    return stop.is_set()
