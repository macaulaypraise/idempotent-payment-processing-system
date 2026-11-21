# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased] — v2 "Ledgerline" (branch `v2`)

v2 rebuilds IPPS as a money-movement core: Postgres-backed idempotency, a
double-entry ledger, a hostile PSP simulator, reconciliation, and a
verification harness. Each build stage is tagged `v2-stage-N`.

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
