"""Workers stop promptly and cleanly on SIGTERM (backlog S7-7).

In a container each worker is PID 1, and the kernel delivers no signal to
PID 1 that it has no handler for: before the handler, `docker stop` waited
the whole grace period and then SIGKILLed. These run each worker as a real
process, wait until it is up, send SIGTERM and expect a clean exit.

The charger and the outbox relay need Kafka to start, which this suite does
not have; they use the same helper (app.core.shutdown).
"""

import os
import selectors
import signal
import subprocess
import sys
import time

import pytest

WORKERS = ("sweeper", "webhook_processor", "idempotency_expiry")


def _wait_for(process: subprocess.Popen[str], text: str, seconds: float) -> str:
    assert process.stdout is not None
    seen = ""
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if selector.select(timeout=0.2):
            line = process.stdout.readline()
            seen += line
            if text in line:
                return seen
        if process.poll() is not None:
            break
    raise AssertionError(f"never saw {text!r}; output:\n{seen}")


@pytest.mark.parametrize("worker", WORKERS)
def test_a_worker_stops_cleanly_on_sigterm(worker: str) -> None:
    env = {
        **os.environ,
        "PSP_BASE_URL": "http://127.0.0.1:9",  # never called: nothing to charge
        "LOG_LEVEL": "INFO",
        "SWEEPER_INTERVAL_SECONDS": "30",  # asleep when the signal lands
        "WEBHOOK_POLL_SECONDS": "30",
        "IDEMPOTENCY_EXPIRY_INTERVAL_SECONDS": "30",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", f"app.workers.{worker}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(process, f"{worker}_started", seconds=30)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        output = _wait_for(process, f"{worker}_stopped", seconds=5)
        code = process.wait(timeout=5)
        assert code == 0, output
        assert time.monotonic() - started < 5  # not the 10 s grace period
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
