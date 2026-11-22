# Ledgerline (IPPS v2)

A Postgres-native money-movement core: an idempotent payments API, a
double-entry ledger, replay-safe provider webhooks and daily reconciliation,
built against a deliberately hostile payment-provider simulator. Its
correctness claims, **no duplicate effect and books that always balance**,
are checked by generated adversarial histories and fault injection on the
full stack, every night, not by a load test.

Each claim below links to the command that reproduces it. Every number was
measured, and says where.

| | Claim | Measured | Reproduce |
|---|---|---|---|
| 1 | **0 duplicate charges, 0 ledger imbalance** across generated adversarial histories and fault-injected runs of the whole stack | 361 state-machine histories (9,460 steps checked, 1,140 provider charges) and 8 fault scenarios (3,462 requests, 41 network faults on Postgres, Redis, Kafka, the provider and the API, 17 process kills, crashes mid-charge): 0 violations. Developer laptop (WSL2), 2026-11-22; both re-run [every night](../../actions/workflows/nightly.yml) | `make verify`, `make verify-faults` |
| 2 | **Redis is a cache, not the source of truth**: losing it creates no duplicates | 1,000 concurrent requests with one idempotency key, Redis stopped: 1 payment, 1 provider charge. Under load: 200 users for 90 s with Redis stopped for 31 s of it: 6,986 requests, 0 duplicate payments (9 requests failed at the client with connection errors; the 2 vCPU VM was overloaded, p50 5 s), 2026-11-22 | `make demo` (step 2), `make load-test-redis-kill`; [docs/benchmarks.md](docs/benchmarks.md) |
| 3 | **The provider can lie and the books still reconcile**: charge-then-timeout, duplicate, late and out-of-order webhooks change nothing twice; every injected settlement discrepancy is flagged and none is posted | every harness history ends with reconciliation and 0 unexplained items; amount mismatch, missing, extra and duplicate lines each detected | `make demo` (steps 3–5), `pytest tests/integration/test_reconciliation.py tests/integration/test_webhooks.py` |
| 4 | **Latency and lag, measured, with the hardware** | 2 vCPU / 8 GB Linux VM: create p50 170 ms / p99 1,000 ms at 20 users, outbox lag p99 478 ms at 50, about 22 captures/s. WSL2 laptop (3 CPUs, 3.8 GiB): create p99 1,800 ms at 50 users, outbox lag p99 281 ms, 11.7 captures/s. Captures are bounded by one sequential charger (DESIGN.md §11). Every service and the load generator on one machine, 2026-11-22: a baseline for changes, not a capacity figure | `make bench`; [docs/benchmarks.md](docs/benchmarks.md) |

The verification log, including the bugs it found (one in the system's
retry ordering, two found by fault injection, six in the harness itself), is
in [docs/verification.md](docs/verification.md).

## What this is not

- **Not a payment service provider.** It never touches card data; payment
  methods are opaque simulator tokens. No PCI scope.
- **One provider, simulated** (`psp_sim/`). No routing; a real provider will
  have behaviours the simulator lacks.
- **Full refunds only, one currency per transfer**, no FX.
- **Not hosted.** The local one-command stack is the deliverable; there is no
  live demo URL (DESIGN.md §13).
- **Verification is not proof.** It shows no violation in the histories
  explored. It is not a linearizability check.

## Quick start

Needs Docker, Python 3.12+ and Poetry.

```bash
make setup          # env files, dependencies, Postgres + Redis, test database
make verify-quick   # state machine (20 histories) + its self-test: ~1 minute
```

That is a fresh clone to a passing verification run. Then:

```bash
make dev            # the whole stack: API :8000, simulator :8001, Kafka, workers, Prometheus, Grafana
make demo           # the three-minute demo below, every claim checked live
make test           # 530+ tests, unit and integration
make verify         # 300 random histories + self-test (~6 min)
make verify-faults  # 8 fault scenarios on the full stack behind Toxiproxy + self-test (~10 min)
make bench          # latency, outbox lag, time to capture, with the hardware
```

## The demo

`make demo` runs against `make dev` and checks each step against the
database and the provider's own records, not the API's answers:

1. 1,000 concurrent requests with one idempotency key: one payment.
2. Redis stopped, the same again: still one payment.
3. The provider charges, then never answers: the payment ends `CAPTURED`
   with one charge, because every retry sends the same provider key.
4. The provider's webhooks redelivered twice each, then late copies under new
   event ids, the refund's before the charge's: no state change, no ledger
   entry posted twice.
5. Reconciliation with one injected amount mismatch: flagged and left
   unsettled; every other line settled with its fee.

## How it works

```
            ┌────────────── Idempotency-Key ──────────────┐
 client ──► │ API: one Postgres transaction               │ ──► Redis (response cache only)
            │ key row + payment + ledger + outbox row     │
            └──────────────────────┬──────────────────────┘
                                   │ Postgres (source of truth)
          ┌────────────────────────┼─────────────────────────────┐
          ▼                        ▼                             ▼
   outbox relay x2          sweeper (retries,              webhook processor
   (one leader, NOTIFY)     leases, lost events)           (inbox, SKIP LOCKED)
          │                        │                             ▲
          ▼ Kafka payments.v1      ▼                             │ signed webhooks
       charger ──── provider key = payment / refund id ───► PSP simulator
                                                                 │
                         reconciliation (daily file) ◄───────────┘
```

- **Idempotency is a database property.** The key row, the payment, its
  ledger postings and its outbox event commit together. A retry replays the
  stored response; a different body under the same key is 422; a key still
  in flight is 409; a database outage is 503. All with `Retry-After` where a
  retry helps.
- **Every provider call uses a stable key** (the payment or refund id), so a
  retry after a lost response gets the original charge back.
- **One function applies every provider outcome** (`apply_psp_result`),
  whether it came from the charger, a webhook, the sweeper or
  reconciliation: it ignores what is already applied.
- **The ledger is double-entry and immutable.** Transfers must balance (a
  deferred trigger), entries cannot be updated or deleted (not even by the
  owner), and cached totals are checked against entry sums.
- **Reconciliation flags, never corrects.**

| Endpoint | |
|---|---|
| `POST /v1/wallets`, `GET /v1/wallets/{id}/balance`, `GET /v1/wallets/{id}/entries` | wallets on the ledger |
| `POST /v1/payments`, `GET /v1/payments/{id}` | pay-in (202, processed asynchronously) |
| `POST /v1/refunds`, `GET /v1/refunds/{id}` | full refunds |
| `POST /v1/transfers` | wallet to wallet |
| `POST /v1/webhooks/psp` | signed provider webhooks |
| `POST /v1/reconciliations`, `GET /v1/reconciliations/{id}` | platform keys only |

Every write needs `Authorization: Bearer <key>` (`make tenant NAME=acme`)
and an `Idempotency-Key`. OpenAPI at http://localhost:8000/docs.

## Read more

- [DESIGN.md](DESIGN.md): invariants, data model, flows, failure modes,
  measured limits.
- [docs/adr/](docs/adr/): nine decisions, each ending with "what would change
  my mind".
- [docs/verification.md](docs/verification.md): what is checked, what is
  not, results, and the bug log.
- [docs/reconciliation.md](docs/reconciliation.md) and
  [docs/runbooks/](docs/runbooks/): Redis down, dead letters growing,
  reconciliation mismatch.
- [docs/writeups/01-testing-a-payment-system.md](docs/writeups/01-testing-a-payment-system.md):
  how the verification was built, and what it found.
- [docs/benchmarks.md](docs/benchmarks.md): the measured runs behind the
  numbers above, with their machines.
- [CHANGELOG.md](CHANGELOG.md): stage by stage.

Python 3.12 · FastAPI · PostgreSQL 16 · Redis 7 · Kafka 4.2 (KRaft) ·
SQLAlchemy (async) · Alembic · Hypothesis · Toxiproxy · Locust · Prometheus ·
GitHub Actions. v1, the original idempotent payments API, is tagged `v1.0.0`.
