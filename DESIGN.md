# DESIGN.md — Idempotent Payment Processing System

## Problem Statement

Payment APIs face a fundamental reliability problem: **the network cannot confirm success**. When a client sends a payment request and the connection drops before receiving a response, neither side knows if the charge was applied. The client retrying risks a double charge. Not retrying risks a missed payment.

A second problem compounds this: **you cannot atomically write to two independent systems**. Writing the payment record to PostgreSQL and publishing an event to Kafka are separate operations. A crash between the two leaves your systems in an inconsistent state — your database says the payment happened but no downstream service ever learns about it.

This system solves both problems explicitly, without relying on distributed transaction coordinators or cloud-specific primitives.

---

## Constraints

- Retrying the same payment request any number of times must produce exactly one charge
- Duplicate charge rate must be 0% under concurrent load
- Events must never be lost, even if Kafka is temporarily unavailable when a payment is created
- P95 latency on `POST /payments` must remain under 50ms at 50 concurrent users
- Payment state transitions must be validated at the application layer — invalid states must be impossible
- All configuration via environment variables — no hardcoded connection strings or secrets

---

## Key Decisions

### Idempotency Keys {#idempotency-keys}

**Chosen:** Client-generated UUID in `Idempotency-Key` header; server caches `key → full HTTP response` in Redis with 24-hour TTL.

**Alternatives considered:**
- Server-generated keys — rejected because if the response is lost in transit, the client has no key to retry with
- Database unique constraint only — works but adds write contention on every request; doesn't handle the concurrent duplicate window (see Distributed Lock below)
- Store only payment ID in cache — rejected because if the original request failed, a retry would receive a success response for a failed payment

**Why this choice:** The client holding the key means it can always ask "did my payment go through?" by replaying the exact same request. Storing the full response (status code + body) means the retry receives an identical answer regardless of what the original outcome was. This matches Stripe's published idempotency design.

---

### Distributed Lock {#distributed-lock}

**Chosen:** Redis `SETNX` on `lock:{idempotency_key}` with a 30-second TTL; release in `finally` block.

**Alternatives considered:**
- Rely on idempotency cache alone — a cache miss followed by two concurrent requests both proceeding creates duplicate payments before either writes to the cache
- Database `SELECT FOR UPDATE` — creates lock contention at the DB layer; more expensive than a Redis round-trip
- Optimistic lock at the idempotency key level — complex to implement correctly with async clients

**Why this choice:** The lock window is tiny (only during the DB transaction + cache write). The TTL guarantees the lock is always released even if the process crashes mid-flight. The `finally` block guarantees release on both success and error paths.

---

### Outbox Pattern {#outbox-pattern}

**Chosen:** Write `OutboxEvent` row inside the same DB transaction as the `Payment` row. Background poller reads `WHERE published_at IS NULL` and publishes to Kafka, then marks as published.

**Alternatives considered:**
- Direct Kafka publish after DB commit — creates a dual-write window: DB succeeds, Kafka publish fails, event is permanently lost
- Two-Phase Commit (2PC) — Kafka does not support distributed transactions; even where supported, 2PC introduces a coordinator as a single point of failure and adds significant latency
- Change Data Capture (CDC) with Debezium — valid at scale but adds operational complexity (Kafka Connect, connector config) disproportionate to this system's size

**Why this choice:** The Outbox pattern requires no distributed transaction coordinator. It makes the event durable (stored in Postgres) before it ever reaches Kafka. If the poller crashes, it restarts and re-reads all `published_at IS NULL` rows — nothing is lost. The tradeoff is 1–5 seconds of latency between payment creation and event consumption, which is acceptable for settlement workflows.

---

### Payment State Machine {#state-machine}

**Chosen:** `PaymentStatus` enum + `VALID_TRANSITIONS` dict in `app/services/state_machine.py`. All status updates call `validate_transition()` before touching the database.

**Valid transitions:**

```
PENDING → PROCESSING
PROCESSING → SETTLED
PROCESSING → FAILED
SETTLED → REFUNDED
FAILED → PENDING  (manual retry)
```

**Why this choice:** Explicit state machines prevent impossible states at the application layer. Without it, a bug in the settlement worker could move a payment from PENDING directly to REFUNDED — a state that has no valid recovery path. The state machine makes this a raised exception rather than silent data corruption.

---

### Optimistic Locking {#optimistic-locking}

**Chosen:** `version` integer column on the `payments` table. Updates include `WHERE version = :current_version`; zero rows affected triggers a retry.

**Alternatives considered:**
- `SELECT FOR UPDATE` (pessimistic locking) — correct but creates contention queues under concurrent updates to the same payment
- `SERIALIZABLE` isolation level — correct but serialization failures require application-level retry logic anyway, with higher overhead

**Why this choice:** Payment status updates are rarely contended — two workers updating the same payment simultaneously is an edge case, not the norm. Optimistic locking lets concurrent reads proceed freely and only pays a retry cost when a real conflict occurs.

---

### Consumer-Side Deduplication {#consumer-deduplication}

**Chosen:** `ProcessedEvent` table with `(event_id, consumer_group)` unique constraint. Event IDs are deterministic UUID5 values derived from `topic + partition + offset`.

**Why this is necessary:** The Outbox poller guarantees at-least-once Kafka delivery. If the settlement worker processes a message and crashes before committing the Kafka offset, the message replays on restart. Without deduplication, this causes a second charge attempt against the payment provider.

**Why UUID5 (deterministic):** The same `topic:partition:offset` always produces the same UUID. This means the deduplication check works correctly on replay without needing to store any state before processing begins.

---

### Technology Choices Summary

| Decision | Chosen | Key Alternative | Why |
|---|---|---|---|
| Idempotency store | Redis + TTL | Postgres unique constraint | Redis is faster; TTL auto-expires old keys |
| Distributed lock | Redis SETNX | DB SELECT FOR UPDATE | In-memory; no DB contention |
| Event bus | Kafka | Redis Streams, RabbitMQ | Durable replay; consumer groups for fan-out |
| Dual-write solution | Outbox pattern | Saga, 2PC, CDC | Simple; no coordinator; no Kafka dependency at write time |
| Balance locking | Optimistic | SELECT FOR UPDATE | No lock queues; retries cheap when conflicts are rare |
| State persistence | PostgreSQL | MongoDB, MySQL | ACID transactions; queryable history |
| Async framework | FastAPI + asyncpg | Django, Flask | Native async; Pydantic validation; auto OpenAPI docs |

---

## Known Limitations

**Outbox poller latency:** Events reach Kafka 1–5 seconds after payment creation, depending on poll frequency. Real-time fraud scoring that needs sub-second event delivery would require a different approach (e.g., synchronous Kafka publish with a fallback circuit breaker).

**Redis as single point of failure:** A Redis outage during a payment burst means idempotency cache misses. Concurrent requests could proceed past the cache check. The distributed lock still prevents duplicate DB rows, but the lock itself depends on Redis. Mitigation: Redis Sentinel or Cluster for HA.

**Outbox poller is single-instance:** Two outbox pollers running simultaneously would both attempt to publish the same events, causing duplicate Kafka messages. The consumer-side deduplication handles this, but the poller should run as a single instance. At scale, use a leader-election mechanism (Redis lock on the poller itself).

**Mock payment provider only:** The settlement worker calls a mock provider. Integrating a real provider (Stripe, Adyen) requires adding their SDK, handling provider-specific error codes, and mapping them to the retry/DLQ logic.

---

## What Would Change at 10× Scale

1. **Redis Cluster** — replace single-node Redis with Redis Cluster for horizontal sharding of the idempotency cache and distributed locks across multiple nodes

2. **Kafka partitioning strategy** — partition `payment.initiated` by `client_id` to ensure all events for one customer are processed in order by the same consumer, preserving state machine consistency

3. **Outbox poller with leader election** — run multiple poller instances with a Redis-based leader lock so that only one publishes at a time, but failover is automatic if the leader dies

4. **Separate read replicas** — route `GET /payments/{id}` to a Postgres read replica to reduce write-node load as read traffic grows

5. **Detection rules as a service** — extract the state machine and retry logic into a separate rule-engine service so they can be updated without redeploying the payment API

6. **Event schema registry** — introduce Confluent Schema Registry to version Kafka message schemas, preventing consumer breakage when event shapes evolve

---

## Deployment {#deployment}

### Railway.app (recommended for portfolio)

```bash
# 1. Push repo to GitHub

# 2. At railway.app:
#    New Project → Deploy from GitHub repo
#    Add Redis service: New → Database → Redis
#    Add PostgreSQL service: New → Database → PostgreSQL
#    Railway auto-injects REDIS_URL and DATABASE_URL

# 3. Set remaining variables in the Variables tab:
#    APP_ENV=production
#    KAFKA_BOOTSTRAP_SERVERS=<upstash-kafka-url>
#    SERVICE_NAME=ipps-payment-api
#    LOG_LEVEL=INFO

# 4. Run migrations via Railway shell:
alembic upgrade head

# 5. Verify:
curl https://<your-railway-url>/health
```

### Post-deployment checklist

- [ ] `/health` returns `{"status": "ok"}` for all services
- [ ] `/docs` loads the Swagger UI
- [ ] `POST /payments` with a UUID key returns 202
- [ ] Same request repeated returns identical `payment_id`
- [ ] `/metrics` shows `payments_created_total` incrementing
