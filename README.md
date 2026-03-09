# Idempotent Payment Processing System

> Retrying the same payment request 1000 times produces one charge, one database row, one Kafka event. Every time.

---

## The Problem This Solves

When a client sends a payment and the network drops before the response arrives — neither side knows if the charge went through. Retry and risk a double charge. Don't retry and risk a missed payment.

This system makes retries safe by solving two problems most payment APIs ignore:

1. **The duplicate request problem** — the same request arriving twice (or 1000 times) must produce exactly one payment
2. **The dual-write problem** — writing to a database and publishing an event cannot be made atomic; a crash between the two creates silent data loss

---

## How It Works

```
CLIENT  →  POST /payments  +  Idempotency-Key: <uuid>
                │
                ▼
        ┌─ Redis cache check ──── HIT → return stored response (no DB touch)
        ├─ Distributed lock ───── prevents concurrent duplicates
        ├─ DB transaction ──────── Payment row + OutboxEvent row (atomic)
        └─ Cache response, release lock → 202 Accepted

OUTBOX POLLER  →  polls outbox_events WHERE published_at IS NULL  →  Kafka

KAFKA  →  SETTLEMENT WORKER
           ├─ PENDING → PROCESSING → SETTLED / FAILED
           ├─ Exponential backoff, max 5 retries
           └─ Dead Letter Queue on exhaustion
```

---

## Results

### Correctness (what actually matters)

| Scenario | Concurrent Users | Requests | Duplicate Charges |
|---|---|---|---|
| Normal load | 50 | 1,378 | **0** |
| Stress test | 1,000 | 12,746 | **0** |

Duplicate charge rate held at **0%** under both loads — verified by asserting every retry with the same idempotency key returns the identical `payment_id`.

### Performance

| Scenario | Req/s | P50 | P95 | Error Rate |
|---|---|---|---|---|
| 50 users | 27 | 10ms | 35ms | 0% |
| 1,000 users | 210 | 160ms | 13s | 0.4% |

At 1,000 users the 0.4% error rate is connection pool exhaustion on a single container — idempotency correctness is maintained throughout. In production: horizontal scaling + DB pool tuning.

### Test Coverage

```
91 tests passing  ·  83% coverage  ·  5.28s
```

---

## Engineering Decisions

**Outbox pattern** instead of direct Kafka publish — writing to Kafka after the DB commit creates a crash window where the event is permanently lost. The Outbox writes the event inside the same DB transaction; the poller handles Kafka asynchronously. [→ DESIGN.md](DESIGN.md#outbox-pattern)

**Redis SETNX distributed lock** — two concurrent requests with the same idempotency key both see a cache miss and both proceed without a lock. SETNX ensures only one continues; the TTL releases the lock automatically if the process crashes. [→ DESIGN.md](DESIGN.md#distributed-lock)

**Optimistic locking** on the payments table — `SELECT FOR UPDATE` creates contention queues. A `version` column lets reads proceed freely; only the write detects conflicts and retries. Works because payment conflicts are rare in practice. [→ DESIGN.md](DESIGN.md#optimistic-locking)

**Deterministic UUID5 event IDs** in the settlement worker — the outbox poller delivers at-least-once to Kafka. If the worker crashes after processing but before committing the offset, the message replays. UUID5 derived from `topic:partition:offset` makes the deduplication check a no-op on replay. [→ DESIGN.md](DESIGN.md#consumer-deduplication)

---

## Quick Start

```bash
git clone <your-repo-url> && cd idempotent-payment-system

cp .env.example .env
make dev          # starts 8 Docker services
make migrate      # applies Alembic migrations
```

API live at **http://localhost:8000/docs**

```bash
# Create a payment
KEY=$(uuidgen)
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount": 99.99, "currency": "USD"}'

# Retry — identical response, no second charge
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount": 99.99, "currency": "USD"}'
```

---

## Testing & Observability

```bash
make test-unit        # 5 seconds, no Docker needed
make test             # full suite + coverage report
make load-test-ui     # Locust UI at http://localhost:8089
```

| Tool | URL |
|---|---|
| API docs | http://localhost:8000/docs |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

---

## Stack

Python 3.12 · FastAPI · PostgreSQL 15 · Redis 7 · Kafka · SQLAlchemy (async) · Alembic · Docker Compose · GitHub Actions CI
