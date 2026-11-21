# 0001. Make Postgres the idempotency source of truth; Redis is a cache

- **Status:** Accepted (implemented in Stage 1)
- **Date:** 2026-11-21
- **Stage:** 1

## Context

v1 handles `POST /payments` in this order: Redis cache lookup → Redis `SETNX`
lock → insert payment + outbox row → cache the response in Redis → release
the lock. `payments.idempotency_key` also has a global unique constraint.

What that means under failure (checked against the v1 code on 2026-11-21):

- **Redis down:** every request fails with HTTP 500, because the cache read and
  lock calls raise. Stopping Redis and sending the same key three times gave
  `500 500 500` and 0 payment rows. The unique constraint means **v1 does not
  create duplicates when Redis is down; it stops taking payments.** The real
  defect is availability, plus the fact that the system's correctness story
  runs through a component that is not durable.
- **Same key, different body:** a cache hit returns the first response without
  comparing the request, so a client bug (reused key, new amount) is silently
  answered with the old payment. There is no 422.
- **Response durability:** the only stored response lives in Redis. After a
  Redis restart, a retry is answered by rebuilding the response from the
  current payment row. If the status has changed by then, the replay is not
  identical to the original (this breaks invariant I9).
- **Lock expiry:** the lock has a 30 s TTL. A request slower than that loses
  its lock while it is still running; the unique constraint catches the
  duplicate, but only after a wasted transaction.
- **Scope:** keys are global, not per tenant. v2 adds tenants.

## Decision

1. Add an `idempotency_keys` table keyed by `(tenant_id, key)`. It stores the
   request fingerprint (SHA-256 of method, path and canonical JSON body), the
   status (`in_progress`, `completed`), the response status and body, the
   resource id, and an expiry.
2. Insert the key row with `INSERT … ON CONFLICT DO NOTHING` **in the same
   transaction** as the business rows and the outbox row. The unique index
   is the lock: a concurrent duplicate blocks on it until the first
   transaction commits or rolls back.
3. After the wait: same fingerprint → replay the stored response; different
   fingerprint → 422; missing header → 400; wait longer than `lock_timeout`
   (set with `SET LOCAL`, surfaces as SQLSTATE `55P03`) → 409 with
   `Retry-After`.
4. Deterministic business rejections (e.g. insufficient funds) are stored
   under the key using a savepoint, so a retry gets the same rejection.
   Unexpected errors roll back everything, key row included.
5. Redis becomes a read-through cache of **completed** responses only,
   written after commit. Every Redis call has short timeouts, is wrapped so
   errors are ignored, and sits behind a small circuit breaker. A cache hit
   still compares fingerprints.
6. Remove `acquire_lock` / `release_lock` and the Redis lock keys.
7. Keep `payments` unique on `(tenant_id, idempotency_key)` as a second line
   of defence. Consequence: once a key row has expired and been swept, reusing
   that key for a payment is a stored 422 ("already used and expired"), never
   a second payment. An expired row that has not been swept yet is taken over
   (`UPDATE … WHERE expires_at <= now`), so the same rule applies.
8. Replays carry an `Idempotent-Replayed: true` header so a client can tell a
   replay from a new write.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep Redis as the truth and add Sentinel/Cluster (v1 DESIGN.md plan) | More infrastructure to make a cache durable. Still a second system in the correctness path. |
| Redlock across several Redis nodes | Contested safety under clock drift and GC pauses; the database already provides a correct lock for free. |
| `SELECT … FOR UPDATE` on a pre-created key row | Needs the row to exist first; insert-or-wait on a unique index does the same job in one statement. |
| `SERIALIZABLE` transactions | Correct, but forces retry loops on every write path for a problem a unique index solves directly. |
| Off-the-shelf FastAPI idempotency middleware | Most store keys outside the business transaction (Redis, memory, SQLite), which is the flaw being removed. |

## Consequences

- Redis down → higher latency, same answers. This becomes testable and is
  headline claim 2. The claim must say "0 duplicates **and** the API keeps
  serving", because "0 duplicates" alone was already true of v1.
- Every write costs one extra indexed insert, and a hot key serialises its
  duplicates on a row lock. That is the intended behaviour.
- `idempotency_keys` grows by one row per write; the `idempotency_expiry`
  worker deletes rows past `expires_at` (24 h) in batches with
  `FOR UPDATE SKIP LOCKED`. For payments, a key reused after expiry is
  rejected (decision 7); resources without their own unique key would be
  created again.
- The request path must never call the provider, or the transaction would
  stay open across a network call (see DESIGN rule 1).
- Test fixtures must create the schema with Alembic, not
  `Base.metadata.create_all`, so constraints written in raw SQL exist in tests.

## Results (Stage 1)

- In-process, real Postgres: 1,000 concurrent requests with one key → one
  payment, one outbox row, 1,000 identical responses, with Redis up and with
  Redis unreachable (`tests/integration/test_idempotency.py`).
- Two deliberately broken variants (skip the key claim; skip the fingerprint
  check) are caught by that suite.
- Locust, 200 users, 90 s, Redis stopped for 31 s mid-run (2-core sandbox,
  so latency is not meaningful): 16,640 requests, 5,323 payments, 0
  duplicates seen by clients, 0 keys with more than one payment, no HTTP 5xx;
  3 client-side connection errors (status 0) in the concurrent-copies task.
  Reproduce with `make load-test-redis-kill`; the README number must come
  from a run on a real machine.

## What would change my mind

- Measured p99 for payment creation over 300 ms where the key insert is the
  main cost, with no cheaper fix available.
- A need for idempotency across services that share no database (then a
  dedicated store with the same transactional semantics would be needed).
- Write volume where `idempotency_keys` bloat or vacuum cost becomes a
  measurable problem; then partition it by `created_at` before reconsidering.
