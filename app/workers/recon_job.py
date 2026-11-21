"""Run reconciliation from the command line (or cron).

    python -m app.workers.recon_job --date 2026-11-21 [--date ...]

Prints one JSON summary per date. Exit code: 0 when every run completed
with no unexplained items, 2 when a run has unexplained items (a cron-friendly
alert), 1 when a run failed or another run of the date was in progress.
"""

import argparse
import asyncio
import json
import sys
from datetime import date

import structlog

logger = structlog.get_logger()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.workers.recon_job")
    parser.add_argument(
        "--date",
        dest="dates",
        action="append",
        required=True,
        type=date.fromisoformat,
        help="provider file date (UTC), YYYY-MM-DD; repeat for several, in order",
    )
    return parser.parse_args(argv)


async def run(dates: list[date]) -> int:  # pragma: no cover - wiring
    from app.clients.psp import PspUnavailable, build_psp_client
    from app.config import get_settings
    from app.core.clock import SystemClock
    from app.core.database import AsyncSessionFactory
    from app.core.logging import setup_logging
    from app.services.reconciliation import (
        Reconciler,
        ReconInProgressError,
        SettlementFileError,
    )

    setup_logging()
    settings = get_settings()
    psp = build_psp_client(settings.PSP_BASE_URL, settings.PSP_TIMEOUT_SECONDS)
    reconciler = Reconciler(AsyncSessionFactory, psp, SystemClock(), settings)
    code = 0
    for day in dates:
        try:
            result = await reconciler.run(day)
        except (ReconInProgressError, PspUnavailable, SettlementFileError) as exc:
            print(json.dumps({"file_date": str(day), "error": str(exc)}))
            code = 1
            continue
        print(
            json.dumps(
                {
                    "run_id": str(result.id),
                    "file_date": str(result.file_date),
                    "lines": result.lines,
                    "counts": result.counts,
                    "unexplained": result.unexplained,
                    "posted": result.posted,
                }
            )
        )
        if result.unexplained and code == 0:
            code = 2
    return code


def main() -> None:  # pragma: no cover
    args = parse_args()
    sys.exit(asyncio.run(run(args.dates)))


if __name__ == "__main__":  # pragma: no cover
    main()
