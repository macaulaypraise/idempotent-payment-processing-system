# Backlog

Two sections: known v1 defects (fixed by the stage noted, **not** patched in
v1), and ideas parked so they do not leak into the current stage.

Evidence column: **code** = read from the source; **observed** = reproduced
against the running stack on 2026-11-21 (KRaft Kafka, mock provider, workers).

## Known v1 defects

### Provider boundary (fixed in Stage 4)

| # | Defect | Where | Evidence | Fixed by |
|---|---|---|---|---|
| D1 | A provider **decline is recorded as `SETTLED`**. The mock returns HTTP 200 with `{"status": "failed"}`; the worker only calls `raise_for_status()`. | `app/workers/settlement_worker.py` `process_message` | code | Stage 4 (charger reads the outcome; `apply_psp_result`) |
| D2 | **Retries never run.** On a provider error the worker re-publishes `payment.initiated`; the retry calls `validate_transition(PROCESSING, PROCESSING)`, which raises, so the payment stays `PROCESSING` forever and the DLQ / `FAILED` path is unreachable. | `settlement_worker.py` | observed: `422: Invalid transition: processing → processing` after a provider timeout; payment stuck in `PROCESSING` | Stage 4 (retries scheduled in Postgres, sweeper) |
| D3 | No idempotency key on the provider call, and the mock provider is not idempotent, so a retry after charge-then-timeout would charge twice (masked today by D2). | `settlement_worker.py`, `mock_provider/main.py` | code | Stage 4 (payment id as provider key; `psp_sim`) |
| D4 | If retries were exhausted, the payment would be marked `FAILED` even though the provider may have charged. A timeout means *unknown*, not failure. | `settlement_worker.py` | code | Stage 4 (stays `PROCESSING`; reconciliation decides) |
| D5 | Retry backoff sleeps inside the consumer loop (`asyncio.sleep(2**n)`), blocking the partition, and retries are published straight to Kafka, bypassing the outbox. | `settlement_worker.py` | code | Stage 4 |
| D6 | Provider URL hard-coded to `http://mock-payment-provider:8001/charge`; the configured `PAYMENT_PROVIDER_URL` / `http_client` base URL is unused. | `settlement_worker.py` | code | Stage 4 |
| D7 | The consumer commits its offset in `finally` even when processing failed, so a crash or error drops the message (at-most-once on the error path). | `settlement_worker.py` `run_settlement_worker` | code | Stage 4 (dedup row recorded with the effect; sweeper as backstop) |

### Events and dedup (fixed in Stage 5)

| # | Defect | Where | Evidence | Fixed by |
|---|---|---|---|---|
| D8 | Consumer dedup keys on Kafka topic/partition/offset (`uuid5`), so an event republished by the poller gets a new offset and is **not** deduplicated. | `settlement_worker.compute_event_id` | code | Stage 5 (dedup on outbox event id) |
| D9 | Outbox poller: no `ORDER BY`, no row lock (two replicas double-publish), no message key (no per-payment ordering), topic = event type, and a failed send is skipped while later rows are still marked published. | `app/services/outbox_poller.py` | code | Stage 5 (ADR 0005) |
| D10 | Webhook endpoint accepts any body: no signature, no storage, no dedup, no effect. | `app/routers/webhooks.py` | code | Stage 5 |

### Idempotency and data model (fixed in Stages 1–3)

| # | Defect | Where | Evidence | Fixed by |
|---|---|---|---|---|
| D11 | **Redis down → API down.** Every `POST /payments` returns 500 because the cache read and lock raise. No duplicates are created (the unique constraint holds), but no payments are taken. | `app/services/payment_service.py` | observed: `500 500 500`, 0 rows | ✅ fixed — Stage 1 (ADR 0001) |
| D12 | Same key with a different body silently replays the first response; no fingerprint, no 422. | `payment_service.py` | code | ✅ fixed — Stage 1 |
| D13 | Replays after a Redis restart are rebuilt from the current row, so they can differ from the original response (status moved on). Breaks I9. | `payment_service.py` IntegrityError branch | code | ✅ fixed — Stage 1 (response stored in Postgres) |
| D14 | Idempotency keys are global, not per tenant; no authentication. | `app/models/payment.py` | code | ✅ fixed — Stage 1 |
| D15 | Money is `Numeric(12,2)` in the DB and `float` in the schema, service and outbox payload. | `models/payment.py`, `schemas/payment.py`, `payment_service.py` | code | ✅ fixed — Stage 2 (integer minor units everywhere) |
| D16 | `version` column is never used. | `models/payment.py` | code | ✅ fixed — Stage 2 (dropped) |
| D17 | State machine is enforced only in Python. The DB has an enum but no transition guard. `FAILED → PENDING` is allowed. | `app/services/state_machine.py`, initial migration | code | Stage 3 (DB guard; `FAILED` terminal) |
| D18 | Test fixtures build the schema with `Base.metadata.create_all`, not Alembic, so constraints and triggers written in raw SQL in migrations would be missing in tests. | `tests/conftest.py` | code | ✅ fixed — Stage 1 (fixtures run Alembic) |

### Operability (fix opportunistically, by Stage 8)

| # | Defect | Where | Evidence | Fixed by |
|---|---|---|---|---|
| D19 | Workers never call `setup_logging()`, so their logs are not JSON. | `app/workers/*` | observed | Stage 4/5 when the workers are rewritten |
| D20 | SQLAlchemy engine created with `echo=True`: every statement and its parameters are logged, in production too. | `app/core/database.py` | observed | ✅ fixed — Stage 1 (drive from settings) |
| D21 | `/health` opens a new Kafka admin client on every call (Railway polls it). | `app/routers/health.py` | code | Stage 9 (split liveness/readiness) |
| D22 | CHANGELOG/README claims are stale: `docker-compose.test.yml` (never existed), Python 3.11, PostgreSQL 15, "Redis Sentinel" mitigation. | `CHANGELOG.md`, `README.md`, `DESIGN.md` | code | Stage 8 rewrite |

## Operations

- **Live Railway demo paused (2026-11-21).** The `RAILWAY_TOKEN` secret is no
  longer valid, so Deploy fails on `main` pushes. Disable the Deploy workflow
  in GitHub Actions until hosting is decided at Stage 8; the local
  `make dev` stack is the primary deliverable.

## Found during Stage 1 (not fixed; noted for their stage)

| # | Item | Where | Stage |
|---|---|---|---|
| S1-1 | The API process created a Kafka producer and HTTP client at startup that it never used, so Kafka down meant the API could not start. **Removed in Stage 1.** | `app/main.py` | done |
| S1-2 | `processed_events` PK is `(event_id, consumer_group)`; DESIGN.md says `(consumer_group, event_id)`. Same uniqueness; revisit when dedup moves to outbox ids. | initial migration | 5 |
| S1-3 | `payments.amount` was still `Numeric(12,2)` after Stage 1. **Done in Stage 2** (BIGINT minor units). | `schemas/payment.py` | done |
| S1-4 | Each Redis breaker trip logs one warning per in-flight request; noisy during an outage. Rate-limit the log if it matters. | `services/response_cache.py` | 9 |

## Found during Stage 2

| # | Item | Where | Stage |
|---|---|---|---|
| S2-1 | Coverage under-reported code after a database await: SQLAlchemy's asyncio layer uses greenlets and coverage did not follow them. Fixed with `concurrency = ["greenlet", "thread"]`; earlier coverage figures (83-88%) were understated. | `pyproject.toml` | done |
| S2-2 | No way to fund a wallet through the API until captured payments credit `wallet_pending` and settlement releases to `wallet_available`. | — | 3, 6 |
| S2-3 | Test cleanup truncates append-only tables with `session_replication_role = replica`, which needs a superuser owner (true for docker-compose and CI). A managed Postgres without superuser would need per-test databases instead. | `tests/conftest.py` | 8 |
| S2-4 | Managed Postgres may not let the migration user `CREATE ROLE`; then create `APP_DB_ROLE` by hand once before migrating. | migration `c3a8d1e6f204` | 8 |
| S2-5 | Hot system accounts (`psp_clearing`, `bank`) will serialise posting per tenant and currency once Stage 3 posts captures; measure before optimising. | `services/ledger.py` | 8 |

## Parked ideas (not in any current stage)

- `provectuslabs/kafka-ui` is no longer maintained; `kafbat/kafka-ui` is the
  active fork. Swap when touching compose next.
- Pin all compose images to exact versions (Prometheus, Grafana, Redis and
  kafka-ui are partly floating).
- The Makefile `dev` target prints a Grafana login of admin/admin; read it
  from `.env` instead.
