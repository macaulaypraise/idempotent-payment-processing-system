# Benchmarks

Measured runs behind the README's numbers. Raw Locust output goes to
`tests/load/results/` (not committed); this file keeps the summaries. Every
run states its machine. All of them ran every service and the load generator
on one machine, so they compare changes on that machine; they are not
capacity figures for a deployment.

## `make bench`

Locust `LegitimateUser` (create a payment, retry it with the same key, read
it) against the dev stack (`make dev`). Create latency comes from Locust;
outbox lag and time to capture come from the database, for the payments the
run created (`scripts/bench.py`).

**Machine:** Linux VM, 2 vCPU (Intel Xeon @ 2.10 GHz), 7.8 GiB, Docker 29.10,
Python 3.12. 2026-11-22.

| Load | POST /v1/payments p50 / p95 / p99 | Creates/s | Outbox lag p50 / p99 | Time to capture p50 / p99 | Captures/s |
|---|---|---|---|---|---|
| 20 users, 30 s | 170 / 850 / 1,000 ms | 20.3 | 20 / 277 ms | 2.7 / 5.4 s | 18.2 |
| 50 users, 60 s | 510 / 900 / 1,100 ms | 40.9 | 40 / 478 ms | 33 / 53 s | 21.9 |

No failed requests; no two payments shared a key in either run.

**Machine:** developer laptop, WSL2 on Windows, 3 CPUs allotted (AMD Ryzen 5
PRO 5650U), 3.8 GiB allotted, Docker 29.8, the full dev stack including
Prometheus, Grafana and Kafka UI. 2026-11-22.

| Load | POST /v1/payments p50 / p95 / p99 | Creates/s | Outbox lag p50 / p99 | Time to capture p50 / p99 | Captures/s |
|---|---|---|---|---|---|
| 50 users, 60 s | 950 / 1,500 / 1,800 ms | 27.0 | 92 / 281 ms | 59 / 77 s | 11.7 |

No failed requests (1,550), no shared keys; every payment captured 81 s after
the load stopped. The laptop is slower than the VM here (3.8 GiB for every
service plus monitoring), and shows the same shape: creates outrun the single
charger more than two to one.

**Reading both:**
- The create-latency SLO (p99 < 300 ms, DESIGN.md §9) is missed on this
  machine. Two vCPUs run Postgres, Kafka, Redis, the API (one uvicorn
  worker), six worker processes and Locust.
- The outbox lag SLO (p99 < 1 s) is met.
- Captures stall at about 20/s: `payments.v1` has one partition and the
  charger handles one message at a time. At 50 users payments arrive twice as
  fast as they are captured, so time to capture grows for as long as the load
  lasts. This is the measured throughput limit (DESIGN.md §11).

## `make load-test-redis-kill`

Locust with both `LegitimateUser` and `ConcurrentDuplicateUser` (the same
request five times at once), Redis stopped for the middle third of the run
(`tests/load/redis_kill.sh`).

**Machine:** as above, 2026-11-22.

| Users | Duration | Redis down | Requests | Failed | Duplicate payments (client-seen / in Postgres) | p50 / p99 |
|---|---|---|---|---|---|---|
| 200 | 90 s | 31 s | 6,986 | 9 (client connection errors, status 0) | 0 / 0 | 5.0 / 13 s |

The machine was overloaded at 200 users (latency in seconds; the failures
are the client giving up on a connection, not error responses). The claim
this run supports is the duplicate count, not the latency.

## Re-running on your machine

```bash
make dev
make bench                               # BENCH_USERS=50 BENCH_SECONDS=60
make tenant NAME=loadtest                # export IPPS_API_KEY=<printed key>
make load-test-redis-kill                # USERS=200 DURATION=90
```

`make bench` writes `tests/load/results/bench-<time>.md` with the same table
and the machine's details.
