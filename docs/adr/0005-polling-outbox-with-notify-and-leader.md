# 0005. Publish the outbox with one leader-elected poller, woken by NOTIFY

- **Status:** Proposed
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
2. Add a `BIGSERIAL` sequence column to `outbox_events` (ids are UUIDs) and
   publish in sequence order. Writers lock the aggregate row before inserting
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

## What would change my mind

- Outbox lag p99 above 1 s at expected load with the single leader.
- A hosting platform that only offers pooled connections.
- A need to fan out to systems outside Kafka, where CDC would serve several
  sinks from one change stream.
