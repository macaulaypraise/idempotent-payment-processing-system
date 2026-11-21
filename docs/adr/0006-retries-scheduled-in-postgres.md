# 0006. Schedule provider retries in Postgres, with leases and a sweeper

- **Status:** Accepted (implemented in Stage 4)
- **Date:** 2026-11-21
- **Stage:** 4

## Context

v1's settlement worker handled a failed provider call by sleeping inside the
Kafka consumer loop (`asyncio.sleep(2**n)`, blocking the partition) and then
publishing a fresh `payment.initiated` straight to Kafka, bypassing the
outbox. That retry crashed on `PROCESSING -> PROCESSING`, so retries never ran
(D2). The provider call carried no idempotency key, so a working retry would
have double-charged after a lost response (D3). Exhausted retries marked the
payment `FAILED` even if the provider had charged (D4). Dedup keyed on Kafka
offsets broke when the topic was recreated in Stage 0 (D8, seen on a real
database with 11,045 stale rows).

Kafka has no delayed delivery. A retry topic per delay ("retry-5s",
"retry-1m") is the usual workaround, and it adds topics, consumers and an
ordering problem for every delay step.

## Decision

1. **Every attempt is lease → call → apply.**
   - In a short transaction, claim the row: `PENDING → PROCESSING`, count
     the attempt, and set `next_attempt_at = now + PSP_LEASE_SECONDS` as a
     lease. Commit.
   - With no transaction open, call the provider. The payment id (or refund
     id) is the provider idempotency key, and the call has an explicit timeout.
   - In a short transaction, apply the outcome through `apply_psp_result` and
     record the consumed event (`processed_events`, keyed on the outbox event
     id carried as a Kafka header). Commit.
2. **A timeout or 5xx means "unknown".** Set `next_attempt_at = now +
   backoff` (exponential, equal jitter, capped). The next attempt asks again
   with the same key.
3. **After `PSP_MAX_ATTEMPTS`**, write a `dlq.v1` outbox event, clear
   `next_attempt_at`, and leave the row `PROCESSING`. The provider may have
   charged; reconciliation (Stage 6) decides, never a guess.
4. **A sweeper** claims due rows in one `UPDATE … WHERE id IN (SELECT … FOR
   UPDATE SKIP LOCKED) RETURNING`. That covers scheduled retries, expired
   leases (the charger crashed mid-attempt) and `PENDING` rows older than
   `SWEEPER_PENDING_AFTER_SECONDS` (their event was lost). It then runs the
   same call-and-apply path. Partial indexes restrict both lookups to
   wallet-backed rows, so pre-ledger payments are never scanned or charged.
5. **The Kafka consumer commits its offset after `handle()` even on error.**
   A row is either unclaimed (the sweeper finds old `PENDING` rows) or leased
   (the sweeper retries it), so no payment depends on redelivery.

## Alternatives considered

| Option | Why not |
|---|---|
| Kafka retry topics with fixed delays | More topics and consumers; per-delay ordering; still needs a store to know what is in flight. |
| Sleep-and-republish in the consumer (v1) | Blocks the partition, bypasses the outbox, and loses the retry on a crash. |
| A job queue library (Celery, arq, Dramatiq) | Another broker or Redis on the correctness path; the database already holds the state these rows need. |
| Temporal / DBOS | Durable retries, but the provider call must still be idempotent, which is the hard part. See the workflow-engine ADR (Stage 8). |
| A lock to stop two workers calling the provider for one payment | Unnecessary: the provider key makes duplicate calls harmless, and a lock held across a network call is how pools get exhausted. |

## Consequences

- One mechanism covers retry, crash recovery and lost events. Tests drive
  it with a fake clock and no sleeps.
- The charger handles one Kafka message at a time. A first attempt that
  times out holds the consumer for `PSP_TIMEOUT_SECONDS`. The sweeper runs
  retries ten at a time.
- `next_attempt_at` doubles as a lease. A charger that crashes mid-call is
  retried only after the lease passes, so the lease must comfortably exceed
  the provider timeout (enforced by a config test).
- A dead-lettered payment stays `PROCESSING` and needs reconciliation or a
  human. The `dlq.v1` topic has no consumer yet; Stage 8 adds a runbook.

## Results (Stage 4)

- Charge-then-timeout at 100%: 10 payments in-process, and 20 on the full
  stack (Kafka, real timeouts). All end `CAPTURED` with exactly one charge
  each in the simulator's ground truth.
- Charger killed with `os._exit(137)` at `charger.after_provider_call` and
  restarted: payment `CAPTURED` on attempt 2, still one charge.
- Provider permanently unavailable: retries, then exactly one `dlq.v1`
  event; the payment stays `PROCESSING` with no charge.
- Planted bugs caught by the suite: a fresh provider key per attempt (two
  charges), a timeout treated as a decline, and the dedup row written before
  the effect.

## What would change my mind

- Needing hundreds of retries per second, where polling the sweeper's index
  becomes the bottleneck.
- Multi-step flows with long waits (e.g. 3-D Secure, payouts with holds),
  where a workflow engine's history earns its keep.
- A provider without idempotency keys. Then a lookup by merchant reference
  before every retry, and a lock, would be required.
