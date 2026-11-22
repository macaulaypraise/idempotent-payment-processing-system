# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased] — v2 "Ledgerline" (branch `v2`)

v2 rebuilds IPPS as a money-movement core: Postgres-backed idempotency, a
double-entry ledger, a hostile PSP simulator, reconciliation, and a
verification harness. Each build stage is tagged `v2-stage-N`.

### Stage 7 (part 1 of 2) — Verification harness: model and state machine

**Added**
- `verification/machine.py`: a Hypothesis state machine over the whole
  system in-process (API, PSP simulator, outbox publisher, charger, sweeper,
  webhook processor, reconciliation, real database). Client, worker and
  fault rules; events consumed and webhooks delivered in any order and more
  than once; Redis up or down per history; a quiesce step that drains every
  worker under faults, then after recovery, then reconciles.
- Safety invariants (I1, I2, I3 with provider ground truth, I5, I6) and
  replay identity (I9) after every step; I4, I7, I10, the model and "no
  unexplained reconciliation item" after quiesce.
- `verification/model.py`: the client's view rebuilt from its history;
  end states and wallet balances derived from the provider's ground truth.
- `verification/history.py` (JSONL histories) and `verification/checker.py`
  (`python -m verification.checker --history … --ground-truth …`).
- `verification/database.py`: one table list for resetting, shared with the
  test suite.
- Harness self-test: `app/core/planted_bugs.py` (`fresh_provider_key`,
  never active in production); `test_harness_catches_a_planted_bug` must
  find the double charge.
- `make verify-quick` (PR profile, in every CI run) and `make verify`
  (nightly profile); `.github/workflows/nightly.yml` with a summary and the
  failing history as an artifact; Hypothesis example database cached.
- `docs/verification.md` (what is checked, what is not, bug log), ADR 0009.

**Measured**: nightly profile, 400 histories, 9,026 steps, 1,614 provider
charges, 132 refunds, 0 violations; the planted bug is found in 12–18
histories. Hand-planted bugs: no fingerprint check on the Postgres path
(caught, PR profile), double release under an active refund (caught,
nightly), webhook processor dropping early events (not caught: healed by
the sweeper and reconciliation, documented).

**Fixed in the harness while building it** (bug log in docs/verification.md):
faults cleared before in-flight work met them; Redis always up hid the
Postgres replay path; a fixed worker order hid retry bugs.

**Fixed after the first local nightly run (2026-11-22):** histories were not
replayable under load (`FlakyStrategyDefinition` after 384 histories on
WSL2; reproduced in the sandbox under CPU load). Worker RNGs are fixed per
example, the simulator's seed is drawn by a rule, the harness's sweeper and
webhook processor run one item at a time, the fake clock runs an hour ahead
of the database clock, and lost responses no longer race a wall-clock
timeout: `psp_sim.create_app(delay=…)` lets the harness raise `TimeoutError`
exactly where the response goes missing. New rule: refund a payment that is
refundable now. New test: `test_the_machine_is_deterministic`.

**Fixed after the first nightly run on GitHub (2026-11-22):** the sweeper
attempted claims in physical heap order (`UPDATE … RETURNING` does not keep
the subquery's `ORDER BY`), so "oldest first" was false and outcomes
depended on vacuum timing; the claim is now a CTE returning rows
`ORDER BY due_at, created_at, id` (regression test included). Hypothesis's
auto-loaded `ci` profile made the nightly run derandomized and
database-less on GitHub; profiles now have an explicit parent.

### Stage 6 — Reconciliation

**Added**
- Simulator settlement files: `GET /settlements/{date}` serves the day's
  CSV (`line_id, type, psp_reference, merchant_reference, gross, fee, net,
  currency`; fee 2.9% + 30). Files are deterministic: line ids are provider
  references and random faults are drawn per reference. Faults: amount
  mismatch, missing line, duplicate line, extra (unknown) line, by profile
  rate or injected with `POST|DELETE /admin/settlements/{date}/faults`.
- `settlement_lines`, `recon_runs`, `recon_items`; `settlement_line_id` on
  payments and refunds, set once (trigger-guarded). Migration `b9d5e3f1a274`.
- `app/services/reconciliation.py` (ADR 0008, `docs/reconciliation.md`):
  idempotent load, one transaction per line, match on provider then
  merchant reference, classify as `matched`, `healed`, `amount_mismatch`,
  `missing_in_ledger`, `duplicate_line`, `missing_at_psp` or
  `status_conflict`. Matches post `settlement_charge` (bank net, psp_fees
  fee, psp_clearing gross) and `release` (skipped while a refund holds the
  money), move `CAPTURED` to `SETTLED`; refund lines post
  `settlement_refund`. `PROCESSING` payments and refunds the provider
  settled are healed through `apply_psp_result` / `apply_refund_result`.
  Advisory lock per date; crashed runs are marked `abandoned`.
- `POST /v1/reconciliations`, `GET /v1/reconciliations/{id}`
  (`?classification=`, `?unexplained_only=`), platform API keys only.
- `python -m app.workers.recon_job --date …` and `make recon DATE=…` (exit
  code 2 when items are unexplained).
- Metrics `recon_items_total{classification}`, `recon_unexplained_items`,
  `recon_last_success_timestamp_seconds`; Prometheus alert rules
  `ReconciliationUnexplainedItems`, `ReconciliationStale`
  (`prometheus/alerts.yml`).
- `verification`: I7 (`psp_clearing` and, with no refund in flight,
  `refunds_payable` are zero once everything is reconciled).
- Settings `RECON_PROVIDER`, `RECON_SETTLEMENT_WINDOW_DAYS` (default 1).

**Measured**: 30 payments with 30% of responses lost, every webhook dropped
and the sweeper off left 15 `PROCESSING`; one run over 330 lines → 315
matched, 15 healed, 0 unexplained, all `SETTLED`, `psp_clearing` 0; the
re-run posted 0. Injected amount mismatch, duplicate and extra lines were
classified on the day, and the missing line by the next day's run.

### Stage 5 — Inbox and outbox

**Breaking**
- `POST /v1/webhooks` (unauthenticated placeholder) is removed. Provider
  webhooks go to `POST /v1/webhooks/psp` and must carry a valid
  `PSP-Signature`; set `PSP_WEBHOOK_SECRET` (empty rejects everything).
- Kafka topics: every `payment.*` and `refund.*` event is published to
  `payments.v1` keyed by payment id; `dlq.v1` to `dlq.v1`. Message values now
  include `event_id` and `event_type`. The old per-type topics are no longer
  written or read (anything stranded there is recovered by the sweeper).

**Added**
- Webhook inbox (ADR 0007): HMAC-SHA256 over `"{timestamp}.{raw body}"`,
  ±`PSP_WEBHOOK_TOLERANCE_SECONDS`, constant-time compare, rotation-friendly;
  `webhook_events` with unique `(provider, provider_event_id)`; 200 for new
  and duplicate events, 400 (nothing stored) for bad signatures or malformed
  events, 413 over `WEBHOOK_MAX_BODY_BYTES`.
- `app/workers/webhook_processor.py`: claims due inbox rows with
  `SKIP LOCKED`, applies them through `apply_psp_result` /
  `apply_refund_result` in the same transaction as the status change; parks
  events that cannot apply yet (backoff, then `dead` + `dlq.v1` after
  `WEBHOOK_MAX_ATTEMPTS`); dead-letters contradictions at once; ignores
  unknown types. Compose service `webhook_processor`.
- Simulator webhooks: every charge and refund produces a signed event,
  stored before the response is sent; delivery retried on non-2xx; faults
  `webhook_drop_rate`, `webhook_duplicate_rate`, `webhook_delay_max_seconds`;
  `GET /admin/webhooks`, `POST /admin/webhooks/{id}/redeliver`,
  `POST /admin/webhooks/drain`. Refunds accept and echo `merchant_reference`
  (the charger sends the refund id). Older simulator stores are upgraded in
  place.
- Outbox relay (ADR 0005, now Accepted): `outbox_events.seq` identity column
  (backlog numbered by `created_at`), partial index on unpublished rows,
  statement-level `NOTIFY` trigger; one leader per advisory lock on a
  dedicated asyncpg connection, LISTEN plus `OUTBOX_FALLBACK_POLL_SECONDS`
  fallback, leadership re-checked before every batch. Compose runs two
  replicas. Idempotent producer with `acks=all`.
- Metrics: `webhooks_received_total{result}`,
  `webhooks_processed_total{event_type,result}`,
  `outbox_publish_lag_seconds` (histogram), `outbox_leader`.
- `app/core/backoff.py` (equal-jitter backoff shared by charger and
  processor). Migration `a7f3c2d8e190`.

**Fixed**
- D9: the relay published in no order, could double-publish with two
  replicas, sent no key, and skipped a failed send while marking later rows
  published. It now stops at the first failure and resumes in order.
- D10: the webhook endpoint had no signature, storage, dedup or effect.
- A payment or refund finished by any source now clears `next_attempt_at`,
  so the sweeper never re-drives a row a webhook already settled.

**Measured**: 300 payments, 30% of provider responses lost, webhooks
duplicated and delayed, sweeper off → all `CAPTURED`, one charge each, 65
healed by webhook. Outbox lag p99 394 ms (p50 29 ms). Leader `kill -9` →
standby leading 0.6 s later. NOTIFY wake-up median 5 ms vs a 30 s fallback.
Webhook {charge, refund, duplicates} in all 6 delivery orders → one final
state, nothing dead-lettered.

### Stage 4 — Hostile PSP simulator + charger

**Added**
- `psp_sim/`: a payment provider simulator with idempotent charges and
  refunds (a reused key returns the original result; a reused key with a
  different body is 422), lookup by id and merchant reference, a ground-truth
  endpoint, and seeded fault profiles: decline, slow, unavailable (503),
  charge-then-timeout and refund-then-timeout (record, then hang). Faults hit
  only the first request for a key. State is SQLite on a volume, so it
  survives restarts. Admin API: `GET|PUT /admin/faults`, `POST /admin/reset`.
- `app/workers/charger.py`: lease → call → apply for `payment.initiated`
  and `refund.created`, with the payment/refund id as the provider key. A
  timeout or 5xx schedules a retry in Postgres (exponential backoff with
  jitter). After `PSP_MAX_ATTEMPTS`, it writes one `dlq.v1` event and leaves
  the row `PROCESSING` (ADR 0006).
- `app/workers/sweeper.py`: claims due retries, expired leases and `PENDING`
  rows whose event was lost (`FOR UPDATE SKIP LOCKED`), then re-drives them.
  Pre-ledger payments are never claimed.
- `app/clients/psp.py`: explicit timeouts. Unknown outcomes raise
  `PspUnavailable`; refusals raise `PspRejected`.
- `app/core/failpoints.py`: crash points (`exit` = `os._exit(137)`,
  `raise` for in-process tests), armed by `FAILPOINTS`, never in production.
- Outbox poller sends `event_id` and `event_type` as Kafka headers; the
  charger deduplicates on the outbox id (not on offsets).
- Indexes for the sweeper (migration `e6c1a9d4b582`).
- `verification`: I3 (one payment per key in the database; one provider
  charge per payment in the ground truth).
- ADR 0006: retries scheduled in Postgres. Metric
  `charger_attempts_total{kind,result}`.

**Removed**
- `app/workers/settlement_worker.py`, `mock_provider/`,
  `app/core/http_client.py` and their tests. Compose replaces
  `settlement_worker` and `mock-payment-provider` with `charger`, `sweeper`
  and `psp-sim`.

**Changed**
- Settings: `PSP_BASE_URL` replaces `PAYMENT_PROVIDER_URL`;
  `PSP_MAX_ATTEMPTS` replaces `PAYMENT_MAX_RETRIES`; new `PSP_TIMEOUT_SECONDS`,
  `PSP_LEASE_SECONDS`, `PSP_RETRY_*`, `SWEEPER_*`, `FAILPOINTS`.

**Measured**: charge-then-timeout at 100% → every payment `CAPTURED` with
exactly one charge (10 in-process, 20 on the full stack); charger killed at
the after-charge failpoint and restarted → `CAPTURED`, one charge; provider
down → one DLQ event, payment `PROCESSING`, no charge.

### Stage 3 — Lifecycle on the ledger + refunds

**Breaking**
- `POST /v1/payments` requires `wallet_id` (same tenant, same currency); the
  capture credits that wallet. Unknown wallet is a stored 404, currency
  mismatch a stored 422.
- Payment statuses: `pending → processing → captured → settled`,
  `processing → failed` (terminal), `captured | settled → refunded`. The
  provider accepting a charge is now `captured`, not `settled`; `settled`
  means the settlement file has cleared it (Stage 6).

**Added**
- `apply_psp_result`: the single, idempotent entry point for charge
  outcomes. Locks the payment, ignores an outcome already reflected, refuses
  contradictions (`ConflictingOutcomeError`) and early outcomes
  (`NotApplicableYetError`), posts the capture transfer, writes the outbox
  event, in the caller's transaction.
- Refunds (full only): `POST /v1/refunds` (idempotent) and
  `GET /v1/refunds/{id}`. Requesting a refund locks the payment, checks it is
  captured or settled with no active refund, and holds the amount in
  `refunds_payable` from wallet pending (captured) or available (settled).
  `apply_refund_result` / `confirm_refund` / `fail_refund` post the
  confirmation or return the hold to where the money belongs at that moment.
- `app/services/posting_rules.py`: one pure function per row of the DESIGN
  posting table (all eight rows, including settlement and release for
  Stage 6).
- Database guards: `payments.status` is text with a CHECK; triggers reject
  illegal payment and refund transitions and changes to amount, currency,
  tenant, key or wallet. At most one non-failed refund per payment.
- `verification/invariants.py`: I4 for payments (exactly one capture when
  captured/settled/refunded, none otherwise) and for refunds.
- Metric `psp_results_total{source,outcome,result}`.

**Fixed**
- v1 recorded provider declines as `settled` (D1): the worker now reads the
  outcome from the response body and applies it through `apply_psp_result`.
- The worker skips wallet-less (v1) payments before calling the provider,
  so they can never be charged without being recorded (S3-1).

**Changed**
- Test engine `pool_timeout` 30 s → 120 s: the 1,000-request tests could
  time out under coverage on slower machines.
- Load test users create a wallet first and send `wallet_id`.

**Measured**: posting table exhaustively unit-tested; 30 capture/refund
races, 20 concurrent refund requests (exactly one created), confirm racing
fail (one winner); I1, I2, I4, I5, I6 hold after each.

### Stage 2 — Ledger core

**Breaking**
- Money is an integer number of minor units everywhere: `{"amount": 1050}`
  means 10.50. Floats and numeric strings are rejected with 422. Responses
  return integers. Existing payments are migrated (`amount * 100`).
- The app should connect as the restricted role: set `MIGRATION_DATABASE_URL`
  (owner), `APP_DB_ROLE`, `APP_DB_PASSWORD`, and point `DATABASE_URL` at the
  app role (see `.env.example`).

**Added**
- Append-only double-entry ledger: `accounts`, `transfers`, `entries`
  (ADR 0002). The database enforces it: deferred triggers reject unbalanced
  transfers and transfers with fewer than two entries at COMMIT; triggers make
  `entries`/`transfers` append-only for every role; a CHECK keeps guarded
  wallet accounts from going negative; composite FKs keep entry and account
  currencies equal.
- `post_transfer`: validates, posts once per reference, updates account totals
  with in-place increments in ascending account order (ADR 0003), and turns a
  guard violation into `InsufficientFundsError`.
- Wallets with pending and available accounts; tenant system accounts created
  per currency on first use.
- `POST /v1/wallets`, `GET /v1/wallets/{id}/balance`,
  `GET /v1/wallets/{id}/entries` (newest first, `limit`/`before` cursor),
  `POST /v1/transfers` (idempotent; overdraft, unknown wallet and currency
  mismatch are stored rejections).
- Restricted application role created by the migration; `python -m app.cli
  configure-app-role` sets its password (run by the `migrate` service).
- `verification/invariants.py`: SQL checks for I1, I2, I5, I6.
- Metrics: `idempotent_requests_total{route,outcome}`,
  `ledger_transfers_total{kind}`.

**Changed**
- `payments.version` dropped (never used).
- Idempotent routes share one helper (`app/routers/idempotent.py`).
- Tests run the app as `ipps_app_test` and clean up as the owner.
- Coverage follows greenlets, so async code after a database call is counted.

**Measured**: 1,000 opposing transfer pairs → 0 deadlocks; 200 concurrent
withdrawals from a balance of 10 → exactly 10 succeed; Hypothesis: 40 random
plans per run keep I1, I2, I5, I6.

### Stage 1 — Idempotency truth in Postgres + tenants

**Breaking**
- All business routes are under `/v1` and need `Authorization: Bearer <api key>`.
  `POST /payments` → `POST /v1/payments`; `/webhooks` → `/v1/webhooks`.
- Missing `Idempotency-Key` is now 400 (was FastAPI's 422). Same key with a
  different body is 422. A key still in flight past `lock_timeout` is 409
  with `Retry-After`.
- `amount` is validated as a decimal with at most 2 places and a 3-letter
  upper-case currency; it is never handled as `float`.

**Added**
- `tenants` and `api_keys` (SHA-256 of the key stored; plaintext shown once).
  `python -m app.cli create-tenant | create-api-key | revoke-api-key`, and
  `make tenant NAME=…` / `make api-key TENANT=…`.
- `idempotency_keys` table: fingerprint (SHA-256 of method, path, canonical
  JSON), stored response, 24 h expiry. Written in the same transaction as the
  payment and outbox row; the primary key is the lock for concurrent
  duplicates. Deterministic rejections are stored via a savepoint.
- Replays return the stored status and body plus `Idempotent-Replayed: true`.
- `idempotency_expiry` worker (and compose service) deletes expired keys.
- `tests/load/redis_kill.sh` / `make load-test-redis-kill`: Locust with Redis
  stopped for the middle third of the run, then a duplicate count.

**Changed**
- Redis is a read-through cache of completed responses only: 250 ms socket
  timeouts, errors ignored, and a 5 s breaker after a failure. The Redis
  `SETNX` lock is removed. The API starts without Redis or Kafka.
- `tenant_id` on `payments`, `outbox_events`, `processed_events` (backfilled
  to a default tenant); payments unique per `(tenant_id, idempotency_key)`.
- SQL echo is off unless `DB_ECHO=true`.
- Test fixtures build the schema with Alembic (not `create_all`) and refuse
  to reset a database whose name lacks "test".

**Removed**
- `tests/unit/test_payment_flow.py` (mock-based, tested the Redis lock);
  its cases now run against real Postgres in `tests/integration`.

**Measured** (details in ADR 0001): 1,000 concurrent same-key requests → 1
payment with Redis up and down; Locust with Redis stopped 31 s → 0
duplicates, no 5xx.

### Stage 0 — Baseline and reframe

**Changed**
- Deploy workflow runs only for successful `push` builds of `main` and
  deploys the tested commit; v2 branch builds no longer reach Railway.
- Local Kafka: Confluent 7.6 + Zookeeper replaced by `apache/kafka:4.2.1` in
  KRaft mode. Containers use `kafka:29092`, the host `localhost:9092`.
- `make dev` now runs Alembic migrations through a one-shot `migrate` service
  before the API and workers start.
- Makefile targets fixed (service name `api`, `.venv/bin` paths, Kafka CLI
  path, `make test` matches CI, `typecheck` runs `mypy .`).
- CI Postgres 15 → 16, matching docker-compose.
- `.env.example` completed (`POSTGRES_*`, `GF_ADMIN_PASSWORD`) and corrected
  (Kafka bootstrap address, provider host name).

**Added**
- `app/core/clock.py`: injectable `Clock` with `SystemClock` and `FakeClock`.
- `docs/adr/`: template and draft ADRs 0001–0005 (idempotency truth, ledger
  model, balance updates, Kafka without abstraction, polling outbox).
- `docs/backlog.md`: 22 known v1 defects with evidence and the stage that
  fixes each, including two confirmed against the running stack (retries
  never run; Redis outage takes the API down).
- Placeholder Makefile targets `verify`, `verify-quick`, `verify-faults`,
  `bench`, `recon`, `demo` that fail until their stage lands.
- `.dockerignore`, `.env.test.example`.

**Removed from version control**
- `.vscode/`, `extensions.txt`, `repo-structure.txt`, committed `__pycache__`.

---

## [1.0.0] — 2026-03-04

First production-complete release. All eight build stages from the
Coding Companion Guide implemented and verified.

### Added

**Stage 1 — Environment & Tooling**
- Python 3.11 pinned via `.python-version`
- Poetry for dependency management with separate dev group
- `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-cov` configured in `pyproject.toml`
- `.env.example` documenting all required environment variables
- `.gitignore` excluding `.env`, `__pycache__`, `.mypy_cache`

**Stage 2 — Docker Compose Infrastructure**
- Full local stack: FastAPI app, Redis 7, PostgreSQL 15, Kafka + Zookeeper, Prometheus, Grafana
- Mock payment provider service (90% success / 10% timeout simulation)
- Health checks on all services; `depends_on: condition: service_healthy` throughout
- `docker-compose.test.yml` for isolated integration test runs

**Stage 3 — Configuration Layer**
- `app/config.py` — `pydantic-settings` `Settings` class reads all values from environment
- `@lru_cache` singleton pattern; zero `os.environ` calls outside this file
- `app/dependencies.py` — FastAPI `Depends(get_settings)` injection

**Stage 4 — Database Models & Migrations**
- SQLAlchemy async models: `Payment`, `OutboxEvent`, `ProcessedEvent`
- `Payment` includes `version` column for optimistic locking
- `OutboxEvent.published_at` nullable — NULL means not yet sent to Kafka
- `ProcessedEvent` enables consumer-side deduplication per consumer group
- Alembic migration: `initial_tables` covers all three tables

**Stage 5 — Core Clients**
- `app/core/redis_client.py` — async Redis with `hiredis`, ping on startup, stored on `app.state`
- `app/core/kafka_client.py` — separate `AIOKafkaProducer` and `AIOKafkaConsumer`
- `app/core/http_client.py` — `httpx.AsyncClient` for payment provider calls
- `app/core/database.py` — `AsyncSession` factory, `get_db` async generator dependency

**Stage 6 — Business Logic (Services)**
- `app/services/idempotency.py` — cache lookup, distributed lock (Redis SETNX + TTL), release
- `app/services/payment_service.py` — full orchestration: cache check → lock → DB transaction → cache response → release
- `app/services/state_machine.py` — `PaymentStatus` enum + `VALID_TRANSITIONS` dict; raises `InvalidStateTransitionError` on illegal moves
- `app/services/outbox_poller.py` — polls `outbox_events WHERE published_at IS NULL`, publishes to Kafka, returns `(count, lag)` tuple; sets `outbox_lag_seconds` Prometheus gauge
- `structlog` structured JSON logging in all service functions

**Stage 7 — API Layer**
- `app/routers/payments.py` — `POST /payments` (idempotent create) + `GET /payments/{id}` (with timestamps)
- `app/routers/webhooks.py` — `POST /webhooks` for provider callbacks
- `app/main.py` — lifespan context manager, `/health` endpoint checks Redis + Postgres + Kafka
- Pydantic schemas: `PaymentCreate`, `PaymentResponse` (includes `created_at`, `updated_at`)
- Custom exceptions with registered handlers: `RateLimitExceededError` → 429, `IdempotencyConflictError` → 409, `InvalidStateTransitionError` → 422, `PaymentNotFoundError` → 404
- Prometheus `/metrics` endpoint with `payments_created_total`, `outbox_lag_seconds`, `dlq_depth_total`

**Stage 8 — Background Workers & Observability**
- `app/workers/outbox_poller.py` — polls every 1s, backs off to 2s when empty; never crashes on Kafka error
- `app/workers/settlement_worker.py` — Kafka consumer on `payment.initiated`; state machine transitions; exponential backoff retry; DLQ after max retries; consumer-side deduplication via `ProcessedEvent` table using deterministic UUID5 event IDs
- Grafana dashboard: "Payment Outcomes", "Outbox Lag", "DLQ Depth" panels
- GitHub Actions CI pipeline: lint → type-check → migrate → unit tests → integration tests → coverage ≥ 80%

**Testing**
- 15/15 tests passing across unit and integration suites
- Key tests: `test_idempotent_retry_returns_same_response`, `test_concurrent_duplicate_requests`, `test_invalid_state_transition_raises`, `test_outbox_published_after_poll`
- Locust load test: 50 concurrent users, ~27 req/s, P50 < 11ms, P95 < 35ms, 0% duplicate charge rate

---

## [0.1.0] — 2026-02-17

### Added
- Initial project scaffold: Poetry, pyproject.toml, folder structure
- Docker Compose with Redis and PostgreSQL stubs
