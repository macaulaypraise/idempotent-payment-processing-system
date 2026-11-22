# Ledgerline (IPPS v2): design

This describes the system as built on the `v2` branch. Every number in it
was measured, and says where and with which command. The v1 design
(Redis-locked idempotency, optimistic locking) is in git history at `v1.0.0`.

## 1. Problem

Moving money through an external provider fails in ways one service cannot
see. Responses are lost. The provider charges and then times out. Webhooks
arrive twice, late or out of order. The settlement file disagrees with
internal records. v1 stopped duplicate API requests. v2 keeps the books
correct through all of the above, and shows it with tests that inject those
failures.

## 2. Goals and non-goals

**Goals**
- No duplicate effect: one payment per idempotency key, one provider charge
  per payment, one ledger posting per money event.
- Books always balance, and every balance is derivable from immutable entries.
- The provider and the ledger are reconciled daily; every difference is
  detected and classified, never silently corrected.
- These hold under dependency failures and process crashes, and a test
  harness demonstrates it (section 10).

**Non-goals**
- Not a payment service provider. No card data, no PCI scope.
- One provider, simulated (`psp_sim/`). No routing.
- Full refunds only. One currency per transfer; no FX.
- Single region, single Postgres primary.

## 3. Invariants

The system's contract. The verification harness checks all ten.

| # | Invariant |
|---|---|
| I1 | Every transfer balances: total debits equal total credits, per currency. |
| I2 | Across the ledger, total debits equal total credits. |
| I3 | At most one payment per `(tenant, idempotency key)`; at most one provider charge per payment. |
| I4 | `CAPTURED`, `SETTLED`, `REFUNDED` payments have exactly one capture transfer; `PENDING`, `PROCESSING`, `FAILED` have none. Refund postings match refund status. |
| I5 | Each account's cached totals equal the sums of its entries. |
| I6 | No guarded account (wallet pending, wallet available) goes below zero. |
| I7 | Once every settlement file is reconciled and no refund is in flight, `psp_clearing` and `refunds_payable` are zero. |
| I8 | Every outbox row is eventually published; each consumer applies each event once. (Checked indirectly, through end states.) |
| I9 | Every answer for one idempotency key is identical; a different body under the same key is 422. |
| I10 | Every acknowledged write exists. |

## 4. Architecture

```
            ┌────────────── Idempotency-Key ──────────────┐
 client ──► │ API (FastAPI)  one Postgres transaction:    │ ──► Redis (response cache only)
            │ key row + business rows + ledger + outbox   │
            └──────────────────────┬──────────────────────┘
                                   │ Postgres (source of truth)
          ┌────────────────────────┼─────────────────────────────┐
          ▼                        ▼                             ▼
   outbox relay x2          sweeper (retries,              webhook processor
   (one leader, NOTIFY)     leases, lost events)           (inbox, SKIP LOCKED)
          │                        │                             ▲
          ▼ Kafka payments.v1      ▼                             │ signed webhooks
       charger ──── provider key = payment / refund id ───► PSP simulator
                                                                 │
                         reconciliation (daily file) ◄───────────┘
```

Three rules shape everything:

1. **The request path never calls the provider.** A write is one Postgres
   transaction, so idempotency is a database property.
2. **Postgres is the only source of truth.** Redis and Kafka can be lost and
   rebuilt without affecting correctness.
3. **Every external effect has a stable key.** Provider calls use the
   payment or refund id as the provider's idempotency key, on every attempt.

## 5. Data model

All tables carry `tenant_id`. Money is `BIGINT` minor units plus a currency.

| Table | Purpose | Key constraints |
|---|---|---|
| `tenants`, `api_keys` | Tenant scoping; API keys stored as SHA-256 hashes | unique hash |
| `idempotency_keys` | Key, request fingerprint, status, stored response, expiry (24 h) | PK `(tenant_id, key)` |
| `wallets` | Owns one pending and one available ledger account | |
| `payments` | Lifecycle, provider reference, attempts, `next_attempt_at` (retry time or lease), `settled_at` | status CHECK; transition trigger; immutable amount, currency, wallet |
| `refunds` | Lifecycle and hold source | one non-failed refund per payment (partial unique index) |
| `accounts` | Kind, currency, `debits_posted`, `credits_posted`, guard flag | CHECK on guarded accounts |
| `transfers` | One row per money event | unique `(tenant_id, kind, reference_type, reference_id)` |
| `entries` | Debit or credit lines, amount > 0 | deferred trigger: transfer balances; no UPDATE, DELETE or TRUNCATE, even for the owner |
| `outbox_events` | Events to publish, `seq` identity in commit order per aggregate | |
| `processed_events` | Consumer dedup | PK `(consumer_group, event_id)` |
| `webhook_events` | Inbox of raw provider events | unique `(provider, provider_event_id)` |
| `settlement_lines`, `recon_runs`, `recon_items` | Reconciliation input and results | lines set once |

Account kinds per tenant: `psp_clearing` (the provider owes us), `bank`,
`psp_fees`, `refunds_payable`; per wallet `wallet_pending` and
`wallet_available`. The app connects as a restricted role that cannot change
ledger rows; migrations run as the owner.

## 6. Key flows

### 6.1 Idempotent write (`app/services/idempotency.py`, ADR 0001)

1. A missing key is 400. The fingerprint is SHA-256 of method, path and
   canonical JSON body.
2. Redis is tried for a completed response (250 ms socket timeout, circuit
   breaker). A hit with a matching fingerprint is returned; any Redis error
   is ignored.
3. In one transaction with a short `lock_timeout`, insert the key row
   (`ON CONFLICT DO NOTHING`).
4. If inserted: business writes, ledger postings and the outbox row, the
   response stored on the key row, commit. Then cache the response.
5. If the row existed (after waiting for its owner): same fingerprint
   replays the stored response; a different one is 422.
6. Waiting longer than 2 s is 409 with `Retry-After`.
7. A deterministic rejection (insufficient funds) rolls back to a savepoint
   and is stored under the key: a retry gets the same rejection. An
   unexpected error rolls back everything, key included.
8. A dependency outage (Postgres connection refused, reset or lost) is 503
   with `Retry-After`: the outcome is unknown, so the client retries with the
   same key. (Found as bare 500s by the fault layer; Stage 8.)

### 6.2 Pay-in

`POST /v1/payments` writes the payment (`PENDING`) and `payment.initiated`,
and returns 202. The outbox relay publishes it to Kafka (`payments.v1`).
The charger claims the payment (`PROCESSING`, a lease in `next_attempt_at`),
commits, then calls the provider with the payment id as key. Every outcome,
whether from that call, the provider's webhook, the sweeper or
reconciliation, goes through one function, `apply_psp_result`: it locks the
payment, ignores results already applied, validates the transition, posts
the capture and writes the outbox event, in one transaction.

```
PENDING -> PROCESSING -> CAPTURED -> SETTLED
                     \-> FAILED (terminal)
CAPTURED | SETTLED -> REFUNDED
```

A timeout is "unknown", never "failed". The retry is scheduled in Postgres
(equal-jitter backoff) and driven by the sweeper, which also re-drives
expired leases and `PENDING` payments whose event was lost (after 60 s).
After 6 attempts a `dlq.v1` event is written and the payment stays
`PROCESSING` for reconciliation to decide (ADR 0006).

### 6.3 Posting rules (`app/services/posting_rules.py`)

| Event | Debit | Credit |
|---|---|---|
| Capture confirmed | `psp_clearing` | `wallet_pending` |
| Settlement line (charge) | `bank` (net), `psp_fees` (fee) | `psp_clearing` (gross) |
| Release after settlement | `wallet_pending` | `wallet_available` |
| Wallet-to-wallet transfer | sender `wallet_available` | receiver `wallet_available` |
| Refund requested (hold) | `wallet_pending` if `CAPTURED`, else `wallet_available` | `refunds_payable` |
| Refund confirmed | `refunds_payable` | `psp_clearing` |
| Refund failed (hold returned) | `refunds_payable` | `wallet_pending` if `CAPTURED` now, else `wallet_available` |
| Settlement line (refund) | `psp_clearing` | `bank` |

Totals are updated in place, accounts locked in sorted order (ADR 0003).
Capture, settlement and refund lock the payment row first, so they are
serialized per payment.

### 6.4 Webhook inbox (ADR 0007)

`PSP-Signature: t=<unix>,v1=<hex>`: HMAC-SHA256 over `"{t}.{raw body}"`,
compared in constant time, stale or future timestamps rejected, several `v1`
values accepted for secret rotation. The raw event is inserted with
`ON CONFLICT DO NOTHING` and 200 is returned, duplicates included. The
processor claims rows with `FOR UPDATE SKIP LOCKED` and applies them through
`apply_psp_result`. An event that cannot apply yet (payment not captured,
not committed yet) is parked with backoff; one that contradicts the record
is dead-lettered at once, never applied.

### 6.5 Outbox (ADR 0005)

Two relays; the one holding a session advisory lock publishes, in `seq`
order, with an idempotent producer (`acks=all`). An insert trigger issues
`NOTIFY`; the relay also polls on a fallback interval, which bounds failover.
Order per payment holds because every writer locks the payment row before
inserting its outbox row.

### 6.6 Reconciliation (ADR 0008, `docs/reconciliation.md`)

Each settlement line is matched by provider reference and classified
`matched`, `healed` (the line proves a charge our ledger had not captured
yet: applied through `apply_psp_result`, then matched), `amount_mismatch`,
`missing_in_ledger`, `duplicate_line` or `status_conflict`. Captures older
than the settlement window (1 day) with no line are `missing_at_psp`.
Matched lines post settlement, fee and release. Everything else is flagged,
never posted. Lines are stored once: re-running a date posts nothing, and a
corrected file does not overwrite a flagged line (resolution is manual:
`docs/runbooks/reconciliation-mismatch.md`).

## 7. Failure modes

Each row is exercised by a test; the last column names one.

| Failure | Behaviour | Shown by |
|---|---|---|
| Response lost, client retries | Stored response replayed | `test_idempotency.py`, I9 in both harness layers |
| Concurrent duplicates | Second waits for the first, then replays | `make demo` step 1 |
| Redis down | Cache skipped; correctness unchanged | `make demo` step 2, `make load-test-redis-kill` |
| Postgres down or connection lost | 503 + `Retry-After`; nothing half-written | `test_routers.py`, fault layer |
| Kafka down | Outbox grows; API unaffected; relays resume | fault layer (kafka blackhole, reset) |
| Relay crashes after publish, before marking | Republished; consumers dedupe | `test_outbox_relay.py` |
| Charger crashes before or after the provider call | Lease expires; retry with the same provider key | `test_charger.py`, fault layer crash points |
| Provider charges, then times out | Retry gets the original charge; else webhook; else reconciliation | `make demo` step 3 |
| Duplicate or late webhook | Inbox dedup; already-applied results ignored | `make demo` step 4 |
| Out-of-order webhook | Parked, applied once its payment is ready | `test_webhooks.py` (every order) |
| Forged or stale webhook | Rejected, not stored | `test_webhooks.py` |
| Dropped webhook | Sweeper retries; reconciliation heals | `test_reconciliation.py` |
| Settlement discrepancy | Flagged, alert, not posted | `make demo` step 5 |
| Reconciliation crashes midway | Re-run continues; postings unique per line | `test_reconciliation.py` |
| Worker stopped (deploy) | Finishes the item in hand, leaves its Kafka group or lock | `test_shutdown.py` |

## 8. Key decisions

One page each in `docs/adr/`, each ending with "what would change my mind".

| # | Decision | Instead of | Why |
|---|---|---|---|
| 1 | Postgres is the idempotency source of truth; Redis a cache | Redis as truth (v1) | A cache outage must not create duplicates |
| 2 | Double-entry ledger, TigerBeetle-style posted totals and guard flags | Mutable balance column | Balances become checkable |
| 3 | In-place total updates in sorted account order | Optimistic version column | Multi-account transfers on hot rows without retries; no deadlocks |
| 4 | Kafka for events, no abstraction | A Postgres queue | Fan-out and replay; at this scale a Postgres queue would do, and that is stated |
| 5 | Polling outbox with NOTIFY and one leader | CDC, parallel pollers | No connector runtime; order preserved |
| 6 | Retries scheduled in Postgres | Kafka retry topics | No delayed delivery in Kafka; one path for retry and recovery |
| 7 | Webhook inbox in Postgres | Processing in the request | Ack fast, apply idempotently, park what cannot apply yet |
| 8 | Daily reconciliation that flags, never corrects | Auto-correction | Correcting would hide the bug that caused the difference |
| 9 | Model-based verification plus a fault layer | Load tests as proof | Generated adversarial histories, checked against invariants |

## 9. Observability and SLOs

Prometheus metrics from the API (`/metrics`); structured JSON logs with
tenant and payment ids. Alerts: unexplained reconciliation items, stale
reconciliation (`prometheus/alerts.yml`). Runbooks: `docs/runbooks/`.

| SLO (target) | Measured (`make bench`, see section 11) |
|---|---|
| Payment-create latency p99 < 300 ms | **missed on both bench machines**: p99 1,100 ms at 50 users (41 creates/s), 1,000 ms at 20 users on a 2 vCPU VM; 1,800 ms at 50 users on a 3 CPU WSL2 laptop. Every service and the load generator share those CPUs |
| Outbox publish lag p99 < 1 s | met: p99 478 ms (VM), 281 ms (laptop) at 50 users |
| Webhook processing p99 < 5 s | not measured |
| Payment-create availability 99.9% | not measured (no long-running deployment) |
| Daily reconciliation: completes, 0 unexplained items | checked by every harness history |

**Not done:** worker processes expose no metrics endpoint, so only the API
is scraped (backlog S8-1); no tracing (optional Stage 9).

## 10. Verification

Two layers, one checker (`docs/verification.md`):

1. **State machine** (`verification/machine.py`): Hypothesis generates
   histories of API calls, single worker steps in any order, provider fault
   profiles and Redis up or down, in-process against real Postgres. Safety
   invariants after every step; completeness and a pure-Python model of every
   row and wallet balance after quiesce. Exactly replayable and shrinkable.
2. **Fault layer** (`verification/faults.py`): the containerised system
   behind Toxiproxy. Seeded plans of latency, resets and blackholes on every
   dependency, SIGKILLs and crashes at exact lines, with concurrent clients
   that retry unknown outcomes. Same checker, plus availability and liveness.

Each layer has a self-test: a planted bug (a fresh provider key per retry)
that it must catch, and does, in every run.

**Results:**

| Run | Histories / scenarios | Checked | Violations |
|---|---|---|---|
| State machine, nightly profile, WSL2 laptop, 2026-11-22 | 361 histories | 9,460 steps, 1,140 provider charges, 296 refunds | 0 |
| Fault layer, WSL2 laptop, 2026-11-22 | 8 scenarios + self-test | 3,462 requests (170 unknown, all retried), 41 network faults, 17 kills, 3 crashes at a line, 1,526 charges | 0 |
| Nightly CI (GitHub Actions) | both, every night | summary on each run | green since 2026-11-22 |

**Bugs found**: in the system, the sweeper retried claims in heap order
rather than due order (made fault order depend on vacuum; fixed). By the
fault layer: database outages surfaced as bare 500s, and workers ignored
SIGTERM as PID 1 (both fixed in Stage 8). Six more were in the harness
itself, found by its self-tests. The full log, with failing sequences, is in
`docs/verification.md`.

## 11. Known limitations

- **Throughput is bounded by one sequential charger.** `payments.v1` has one
  partition and the charger handles one message at a time, so captures are
  serial: about 22 captures/s end to end on the bench machine, so at 50
  users (41 creates/s) the queue grows and time to capture reached p50 33 s,
  p99 53 s; at 20 users (20 creates/s) it was p50 2.7 s. Measured with
  `make bench` on a 2 vCPU / 8 GB Linux VM, 2026-11-22. A WSL2 laptop
  (3 CPUs, 3.8 GiB) shows the same shape: 27 creates/s against 11.7
  captures/s (docs/benchmarks.md). The
  `psp_clearing`, `bank` and `psp_fees` hot rows are the next limit, not
  the first; more partitions and chargers come before them.
- Every bench number comes from one machine running every service, the
  load generator included: they compare changes on the same machine, not
  production capacity.
- Idempotency keys expire after 24 hours; a reuse after expiry creates a new
  payment.
- Only the API's metrics are scraped (backlog S8-1).
- Verification shows the absence of violations in the histories explored,
  not a proof, and not a linearizability check. The fault layer reproduces a
  plan, not an interleaving.
- The provider is simulated; a real one will have behaviours the simulator
  lacks.
- Local only: there is no hosted demo (section 13).

## 12. At 10x scale

- Partition `payments.v1` by payment id and run a charger per partition.
- Stop caching totals on system accounts; derive them from entries with
  periodic snapshots.
- Partition the outbox by aggregate hash with one leader per partition, or
  move to CDC.
- Read replicas for balance and history queries; partition `entries` by time.
- Multi-region would need idempotency keys homed per region; out of scope.

## 13. Deployment

Local, one command (`make dev`), and that is the supported way to run it.
A hosted demo needs managed Kafka, a direct (non-pooled) Postgres connection
for the relay's lock and LISTEN, and pollers that do not keep a
scale-to-zero database awake: about $5–10 a month. It is not deployed;
`Deploy.md` is the v1 Railway guide and does not cover v2.
