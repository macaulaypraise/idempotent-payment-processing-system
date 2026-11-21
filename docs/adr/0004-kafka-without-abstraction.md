# 0004. Keep Kafka for domain events, with no broker abstraction layer

- **Status:** Proposed
- **Date:** 2026-11-21
- **Stage:** 0 (local KRaft), 5 (topics and producer settings)

## Context

v1 publishes outbox events to Kafka, using the event type as the topic name
(`payment.initiated`, `payment.completed`, `payment.dlq`) and no message key.
Locally it ran Confluent Kafka 7.6 with Zookeeper; production used Redpanda
Serverless, which is now a time-limited trial rather than a free tier.

v2 adds retry scheduling and a webhook inbox. Both are queues in which order
does not matter and items must wait until a given time. Kafka has no delayed
delivery.

At this project's volume a Postgres queue (`FOR UPDATE SKIP LOCKED`) would be
enough for every use. The question is whether Kafka earns its place.

## Decision

1. Kafka carries domain events that several independent consumers read and
   that are worth replaying: `payments.v1`, `ledger.v1`, `dlq.v1`. Event type
   moves into the payload and a header. The message key is the aggregate id
   (payment id), so events for one payment stay ordered in one partition.
2. Postgres carries work queues: retry scheduling (`next_attempt_at` on the
   row), the sweeper, and the webhook inbox.
3. Code talks to aiokafka directly. No interface that would let Redis Streams
   or a Postgres queue stand in for Kafka.
4. Local development runs single-node Apache Kafka in KRaft mode (done in
   Stage 0; Zookeeper is gone as of Kafka 4.0).
5. Producer settings from Stage 5: idempotence enabled, `acks=all`.

## Alternatives considered

| Option | Why not |
|---|---|
| Postgres queue for everything | Would work at this scale and remove a moving part. Loses independent consumer offsets and replay, and Kafka is what the target fintech roles run. The trade is stated plainly in the README. |
| Kafka for everything, including retries | Needs retry topics with sleep-and-republish (v1's approach), which blocks partitions and breaks ordering. |
| Broker abstraction with swappable backends | Lowest-common-denominator semantics, more code, and hides ordering and delivery guarantees that differ between backends. |
| Kafka share groups (KIP-932, production-ready in 4.2) | Queue semantics without per-key ordering; ordering per payment matters more here. Share groups also archive failed records in place rather than providing a DLQ. |

## Consequences

- Two queueing mechanisms with a clear rule for which is used where.
- Hosting needs a managed Kafka that allows three topics, or the live demo is
  local-only (decided at Stage 8).
- The Stage 7 in-process harness bypasses Kafka on purpose; Kafka behaviour is
  covered by the fault-injection layer.

## What would change my mind

- A hosting situation where no free or cheap managed Kafka fits, and the demo
  matters more than the Kafka signal; then a Postgres queue with an ADR saying
  why.
- Consumers that need neither replay nor fan-out; then a Postgres queue is
  simpler.
- Throughput needs where per-key ordering limits parallelism more than
  partitions can solve.
