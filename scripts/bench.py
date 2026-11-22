"""make bench: latency and lag of the running dev stack, with the hardware.

    make dev
    make bench                       # BENCH_USERS=50 BENCH_SECONDS=60 by default

Runs Locust headless (tests/load/locustfile.py, LegitimateUser: create, retry
with the same key, read) and writes its CSVs to tests/load/results/. Then,
from the database, for the payments the run created:

  create latency   p50 / p95 / p99 of POST /v1/payments (Locust)
  outbox lag       commit of an outbox row -> published to Kafka
  time to capture  API accepted the payment -> captured in the ledger
                   (outbox, Kafka, charger, simulator, posting)

Every number is printed with the machine it was measured on, and written to
tests/load/results/bench-<time>.md. Numbers from a laptop with every service
in Docker say little about production; they say whether a change made this
system slower on the same machine.
"""

import argparse
import asyncio
import csv
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.stack import API_URL, Stack, hardware, wait_healthy

RESULTS = Path("tests/load/results")


def locust(api_key: str, users: int, seconds: int, prefix: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "tests/load/locustfile.py",
        "LegitimateUser",
        "--headless",
        "-u",
        str(users),
        "-r",
        str(max(1, users // 5)),
        "-t",
        f"{seconds}s",
        "--host",
        API_URL,
        "--csv",
        str(prefix),
        "--only-summary",
    ]
    print(f"locust: {users} users for {seconds}s against {API_URL}", flush=True)
    with open(f"{prefix}.log", "w") as log:
        subprocess.run(
            command,
            env={**os.environ, "IPPS_API_KEY": api_key},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,  # exits non-zero on any failed request; reported below
        )


def create_stats(prefix: Path) -> dict[str, str]:
    with open(f"{prefix}_stats.csv") as source:
        for row in csv.DictReader(source):
            if row["Type"] == "POST" and row["Name"] == "/v1/payments":
                return row
    raise SystemExit(f"no POST /v1/payments row in {prefix}_stats.csv")


async def wait_quiet(stack: Stack, since: datetime, seconds: float) -> float:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        left = await stack.scalar(
            "SELECT count(*) FROM payments WHERE created_at >= :since AND "
            "(status = 'PENDING' OR (status = 'PROCESSING' "
            "AND next_attempt_at IS NOT NULL))",
            since=since,
        )
        if left == 0:
            return time.monotonic() - started
        await asyncio.sleep(1.0)
    raise SystemExit(f"payments still in flight {seconds:.0f}s after the run")


async def measure(stack: Stack, since: datetime) -> dict[str, Any]:
    lag = await stack.rows(
        "SELECT count(*), "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY ms), "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY ms) FROM ("
        "  SELECT extract(epoch FROM published_at - created_at) * 1000 AS ms"
        "  FROM outbox_events WHERE created_at >= :since"
        "  AND published_at IS NOT NULL) l",
        since=since,
    )
    capture = await stack.rows(
        "SELECT count(*), "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY ms), "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY ms), "
        "extract(epoch FROM max(captured_at) - min(accepted_at)) FROM ("
        "  SELECT p.created_at AS accepted_at, o.created_at AS captured_at,"
        "    extract(epoch FROM o.created_at - p.created_at) * 1000 AS ms"
        "  FROM payments p JOIN outbox_events o"
        "    ON o.event_type = 'payment.captured'"
        "   AND o.payload->>'payment_id' = p.id::text"
        "  WHERE p.created_at >= :since) c",
        since=since,
    )
    statuses = await stack.rows(
        "SELECT status, count(*) FROM payments WHERE created_at >= :since "
        "GROUP BY 1 ORDER BY 1",
        since=since,
    )
    duplicates = await stack.scalar(
        "SELECT count(*) FROM (SELECT 1 FROM payments WHERE created_at >= :since "
        "GROUP BY tenant_id, idempotency_key HAVING count(*) > 1) d",
        since=since,
    )
    (lag_n, lag_p50, lag_p99) = lag[0]
    (cap_n, cap_p50, cap_p99, cap_span) = capture[0]
    return {
        "outbox_events": lag_n,
        "outbox_lag_p50_ms": lag_p50,
        "outbox_lag_p99_ms": lag_p99,
        "captured": cap_n,
        "capture_p50_ms": cap_p50,
        "capture_p99_ms": cap_p99,
        "capture_per_s": cap_n / cap_span if cap_n and cap_span else None,
        "statuses": {str(s): int(n) for s, n in statuses},
        "duplicate_keys": duplicates,
    }


def _ms(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):,.0f} ms"


def report(args: argparse.Namespace, stats: dict[str, str], m: dict[str, Any]) -> str:
    hw = hardware()
    lines = [
        f"# make bench, {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}",
        "",
        f"Load: {args.users} Locust users for {args.seconds}s "
        "(LegitimateUser: create, retry with the same key, read), "
        f"against {API_URL}, every service in Docker on one machine.",
        "",
        "| Measure | p50 | p95 | p99 | Count |",
        "|---|---|---|---|---|",
        f"| POST /v1/payments | {stats['50%']} ms | {stats['95%']} ms | "
        f"{stats['99%']} ms | {stats['Request Count']} requests, "
        f"{stats['Failure Count']} failed, {float(stats['Requests/s']):.1f}/s |",
        f"| Outbox lag (commit -> Kafka) | {_ms(m['outbox_lag_p50_ms'])} | | "
        f"{_ms(m['outbox_lag_p99_ms'])} | {m['outbox_events']} events |",
        f"| Time to capture (accepted -> captured) | {_ms(m['capture_p50_ms'])} | | "
        f"{_ms(m['capture_p99_ms'])} | {m['captured']} payments |",
        "",
        f"Captured {m['captured']} payments at "
        + (
            f"{m['capture_per_s']:.1f}/s end to end"
            if m["capture_per_s"]
            else "an unmeasurable rate"
        )
        + f"; end states {m['statuses']}; payments sharing a key: "
        f"{m['duplicate_keys']}.",
        "",
        "Hardware: " + "; ".join(f"{k} {v}" for k, v in hw.items()),
    ]
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    await wait_healthy()
    stack = Stack()
    try:
        api_key = await stack.platform_api_key()
        since = await stack.scalar("SELECT now()")
        RESULTS.mkdir(parents=True, exist_ok=True)
        prefix = RESULTS / f"bench-{datetime.now():%Y%m%d-%H%M%S}"
        locust(api_key, args.users, args.seconds, prefix)
        stats = create_stats(prefix)
        settled_in = await wait_quiet(stack, since, args.quiet_seconds)
        print(f"every payment processed {settled_in:.0f}s after the load stopped")
        measured = await measure(stack, since)
        text = report(args, stats, measured)
        Path(f"{prefix}.md").write_text(text)
        print()
        print(text)
        print(f"written: {prefix}.md (and Locust CSVs next to it)")
        return 1 if measured["duplicate_keys"] else 0
    finally:
        await stack.close()


def main() -> None:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m scripts.bench")
    parser.add_argument(
        "--users", type=int, default=int(os.environ.get("BENCH_USERS", "50"))
    )
    parser.add_argument(
        "--seconds", type=int, default=int(os.environ.get("BENCH_SECONDS", "60"))
    )
    parser.add_argument("--quiet-seconds", type=float, default=180.0)
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
