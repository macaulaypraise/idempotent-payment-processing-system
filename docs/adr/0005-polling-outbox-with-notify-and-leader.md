# 0005. Publish the outbox with one leader-elected poller, woken by NOTIFY

- **Status:** Accepted (implemented and measured in Stage 5)
- **Date:** 2026-11-21
- **Stage:** 5

## Context

v1's outbox poller selects up to 100 unpublished rows **with no `ORDER BY` and
no row lock**, sends each with no Kafka key, and commits. Consequences, from
the code:

- Two poller replicas would both read and publish the same rows.
- Rows are not published in insertion order, and without a key events for one
  payment can land on different partitions, so consumers can see
  `payment.completed` before `payment.initiated`.
- A send that fails is logged and skipped, while later rows in the same batch
  are still marked published, so ordering breaks on transient errors.
- When idle it sleeps 2 s between polls, which bounds publish latency from
  below.

Consumer dedup in v1 keys on Kafka topic, partition and offset, so a
republished event (poller crash after send, before commit) gets a new offset
and is **not** deduplicated.

## Decision

1. Exactly one poller publishes at a time. It holds a session-level advisory
   lock (`pg_try_advisory_lock`) on a dedicated, direct (non-pooled) asyncpg
   connection. If that connection drops it stops publishing immediately;
   another replica takes the lock.
2. Add a sequence column to `outbox_events` (ids are UUIDs) and publish in
   sequence order. (Implemented as `seq BIGINT GENERATED ALWAYS AS
   IDENTITY`; rows already waiting at migration time are numbered by
   `created_at` first, so an existing backlog drains oldest first.) Writers lock the aggregate row before inserting
   its outbox row, so sequence order follows commit order per aggregate.
3. A statement-level trigger issues `NOTIFY` on insert. The leader `LISTEN`s
   on the same connection and wakes on a notification or a fallback timeout,
   then calls `run_once()` until it returns 0. Notifications are lost while
   disconnected, so the fallback poll is required, not optional.
4. `run_once()` sends a batch with the aggregate id as key and stored headers,
   waits for acknowledgements, marks the rows published, and commits. On a
   send failure it stops the batch rather than skipping ahead.
5. Consumers deduplicate on the outbox event id carried in the message, not
   on Kafka coordinates. At-least-once publish plus idempotent consumers is
   the guarantee.

Topics are consolidated at the same time: every `payment.*` and `refund.*`
event goes to `payments.v1` keyed by payment id, `dlq.v1` has its own topic,
and `ledger.v1` is reserved. The producer is idempotent with `acks=all`.

## Alternatives considered

| Option | Why not |
|---|---|
| CDC (Debezium / logical replication) | Needs a connector runtime and replication-slot operations; free Kafka tiers exclude Kafka Connect. Documented as the scale-up path. |
| Parallel pollers with `SKIP LOCKED` | Higher throughput but breaks per-aggregate ordering. `SKIP LOCKED` is used for the inbox and sweeper, where order does not matter. |
| Leader election via a Redis lock (v1 DESIGN.md plan) | Puts Redis back on a correctness path; the database already offers advisory locks. |
| Poll only, with a shorter interval | Lower latency costs constant queries; NOTIFY gives near-immediate wake-up for free. |

## Consequences

- Publish latency drops from up to 2 s to roughly one round trip after commit
  (outbox lag p99 is measured in Stage 5; SLO target < 1 s).
- Throughput is bounded by one publisher. Acceptable at this scale; the 10x
  plan is to partition the outbox by aggregate hash with a leader per
  partition, or move to CDC.
- The poller cannot run behind a transaction-mode connection pooler (breaks
  `LISTEN` and session advisory locks). This constrains hosting (Stage 8).
- Raw asyncpg needs a `postgresql://` URL, not SQLAlchemy's
  `postgresql+asyncpg://`.

## Results (Stage 5)

Measured in the sandbox: API with 2 workers, simulator, two relay replicas,
charger and webhook processor as host processes against Postgres 16 and
single-node KRaft Kafka 4.2.1; 300 payments from 20 concurrent clients, with
30% of provider responses lost and webhooks duplicated and delayed.

| Measure | Result |
|---|---|
| Outbox lag (`published_at - created_at`, 600 events) | p50 29 ms, p95 251 ms, **p99 394 ms**, max 503 ms |
| NOTIFY wake-up, idle leader, fallback poll 30 s (integration test, 10 inserts) | median 5 ms, max < 1 s |
| Failover after `kill -9` of the leader (fallback poll 2 s) | standby leader after 0.6 s; nothing lost |
| Messages on `payments.v1` after the run | 600 for 600 rows (no duplicates; the kill happened while idle) |
| Leader-only publishing (integration test, two relays, 40 events) | leader 40, standby 0 |

The p99 is within the < 1 s target, but it is a sandbox number with sends
acknowledged one at a time; the user's WSL2 numbers go in STAGE-5.md. Lag
includes the time from building the row to the request's COMMIT, because
`created_at` is set when the row object is created.

What the tests pin: publishing stops at the first failed send and resumes in
order (D9); only the lock holder publishes; a standby takes over within one
fallback interval after a clean stop or after `pg_terminate_backend` kills
the leader's session; a NOTIFY wakes an idle leader long before its fallback
poll; seq order matches lifecycle order for one payment.

Known gaps:
- Sends are sequential (one `send_and_wait` per row). Pipelining would raise
  throughput but can reorder a payment's events if an early send fails after
  a later one succeeds; ordering wins until throughput is measured as a
  problem (Stage 8).
- A leader whose lock session died mid-batch can finish that batch while the
  new leader starts: duplicates, never loss or reorder within one relay.
  Consumers deduplicate on `event_id`.
- If the leader host vanishes without closing TCP, Postgres releases the lock
  only when it notices the dead session; set `tcp_keepalives_*` (or
  `idle_session_timeout`) when hosting (Stage 8).
- `payments.v1` is auto-created with one partition. Create it with explicit
  partitions in Stage 8.

## What would change my mind

- Outbox lag p99 above 1 s at expected load with the single leader.
- A hosting platform that only offers pooled connections.
- A need to fan out to systems outside Kafka, where CDC would serve several
  sinks from one change stream.
