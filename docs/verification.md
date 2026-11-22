# Verification

"Tests pass" says the cases someone thought of work. This harness says
something stronger: **these invariants held across N generated adversarial
histories**, and it proves it can tell, by catching a bug planted on purpose.

Two layers, one checker:

1. **The state machine**: generated histories, in-process, sequential and
   exactly replayable.
2. **The fault layer**: the whole system in containers behind Toxiproxy,
   with seeded network faults, process kills and crash points. Real
   concurrency, real Kafka, real time; reproducible plans, not histories.

Code: `verification/` (library), `tests/verification/` (entry points),
`docker-compose.faults.yml`. Decision record: ADR 0009.

## Running it

```bash
make verify-quick    # ~20 derandomized histories + self-test (~30 s); runs in every CI build
make verify          # 300 random histories + self-test (~6 min); the nightly job
VERIFY_EXAMPLES=1000 make verify
make verify-faults   # fault self-test + 8 fault scenarios against the full stack (~10 min); nightly
FAULT_SEEDS=1004 make verify-faults     # one plan again
```

`verify-quick` and `verify` need Postgres and Redis, like `make test`; on a
fresh clone, `make setup` provides both and writes `.env.test`.
`verify-faults` needs only Docker: it builds and starts its own stack
(next section). `make verify` writes the
last history to `.verify/last.jsonl` (and the provider's ground truth next
to it). A failing run prints the minimal failing sequence of steps;
Hypothesis keeps it in `.hypothesis/` and replays it first next time.

## What one history is

A Hypothesis state machine (`verification/machine.py`) drives the real
system in-process, against the real database:

| Kind | Rules |
|---|---|
| Client | create wallet (up to 3), create payment, retry it with the same key and body, retry it with the same key and a different body, request a refund of any payment (mostly too early: 422), refund a payment that is refundable now, retry a refund, transfer between wallets |
| Worker | publish one outbox batch, consume any queued event (in any order, sometimes leaving it queued to be delivered again), sweep with the clock advanced 5 s / 90 s / 400 s, deliver any pending webhook (in any order, sometimes twice), run the webhook processor, reconcile |
| Fault | switch the provider to: no faults, lost responses, declines and failed refunds, unavailable, slow, webhook chaos (drop, duplicate, delay + lost responses); Redis up or down for the whole history |
| Quiesce | one pass of every worker under the current faults, then the provider recovers and every worker runs until nothing changes; reconcile every date (sweeper first or webhooks first, either order) |

Every history starts with the provider already misbehaving and one to
three payments in flight, and ends with a quiesce.

The API runs through httpx's ASGI transport, the PSP simulator the same
way, the workers through their single steps (`charger.handle`,
`sweeper.run_once`, `WebhookProcessor.run_once`, `Reconciler.run`). Kafka is
replaced by a queue fed by the real outbox publisher. Time is a fake clock
shared by everything, including webhook signatures.

## What is checked

| When | Check | Source |
|---|---|---|
| after every step | I1 every transfer balances | `invariants.i1_transfers_balance` |
| after every step | I2 the whole ledger balances | `invariants.i2_ledger_balances` |
| after every step | I3 one payment per key; **one provider charge per payment** (provider ground truth) | `invariants.i3_*` |
| after every step | I5 account totals equal entry sums | `invariants.i5_account_totals_match_entries` |
| after every step | I6 no guarded wallet account below zero | `invariants.i6_guarded_accounts_not_negative` |
| after every step | I9 a retry with the same key and body gets the identical answer; a different body gets 422 | `checker.replays` |
| after quiesce | I4 captures and refund postings match statuses | `invariants.i4_*` |
| after quiesce | I7 `psp_clearing` (and `refunds_payable` with nothing in flight) is zero | `invariants.i7_clearing_accounts_settled` |
| after quiesce | I10 every acknowledged payment and refund exists | `model.quiescent_violations` |
| after quiesce | the model: every row and balance is what the history and the provider imply | `model.quiescent_violations` |
| after quiesce | reconciliation left no unexplained item | `checker.unexplained_reconciliation_items` |

**The model** (`verification/model.py`) is rebuilt from the client's
history alone: what was acknowledged, each write once however often it was
retried. It never predicts a single state, because processing is
asynchronous. Once the system is quiet it asks the provider's ground truth
what happened and derives what must be true:

- charged → `SETTLED` or `REFUNDED`; declined → `FAILED`; never charged
  (retries exhausted) → `PROCESSING`, dead-lettered;
- refund succeeded / failed / never sent → `SUCCEEDED` / `FAILED` / in flight;
- for every wallet: pending = 0, and available = charged payments − refunds
  not `FAILED` + transfers in − transfers out, to the minor unit.

That last line ties three independent sources together: the ledger, the
provider's records and the client's acknowledged transfers. A double
release, a lost capture or a transfer applied twice all break it.

The fault layer calls the same `check_all` on its recorded histories. A
failing scenario can be re-checked offline against the database it left
behind:

```bash
python -m verification.checker --history .verify-faults/<seed>/history.jsonl \
  --ground-truth .verify-faults/<seed>/ground-truth.json \
  --database-url postgresql+asyncpg://postgres:postgres@localhost:55432/payments_faults
```

## Does it catch bugs? (the self-test)

`test_harness_catches_a_planted_bug` runs the machine with a planted bug,
the charger sending a fresh provider key on every retry
(`app/core/planted_bugs.py`; impossible to enable in production), and must
fail with a double charge (I3). It runs in every CI build and every night.

Other bugs were planted by hand while building the harness (code edited,
harness run, edit reverted):

| Planted bug | Caught? | By |
|---|---|---|
| Fresh provider key per retry | yes, in every profile (after 11–18 histories) | I3 |
| No fingerprint check on the Postgres replay path | yes, PR profile | I9 (only once histories ran with Redis down; see the log below) |
| Release on settlement while a refund holds the money | yes, nightly profile only | the wallet guard (I6) refused the double release |
| Webhook processor marks a not-yet-applicable event processed instead of parking it | **no** | nothing is wrong afterwards: the sweeper and reconciliation heal what the webhook would have. Defence in depth makes this bug invisible to invariants; it is covered by the Stage 5 tests instead |

## The fault layer

`make verify-faults` starts `docker-compose.faults.yml`: every process in
its own container (API, simulator, two outbox relays, charger, sweeper,
webhook processor) and Toxiproxy in front of every dependency.

```
client (runner) ──► toxiproxy ──► api ◄── toxiproxy ◄── psp-sim webhooks
app + workers   ──► toxiproxy ──► postgres | redis | kafka | psp-sim
```

Kafka advertises the proxy's address, or clients would go around it after
the first metadata call. The stack is its own compose project
(`ipps-faults`: own containers, ports, tmpfs database), so it cannot touch
the dev stack or its data; every scenario truncates its database. Retries,
leases and the sweeper interval are shortened so a scenario quiesces in
seconds. The runner reaches Postgres, Redis and the simulator directly for
resets, ground truth and checks: the control plane is never faulted.

**A scenario** (`verification/scenario.py`) is a seed that expands into a
40-second plan:

| Fault | How | Targets |
|---|---|---|
| latency | Toxiproxy `latency`, 0.3–3 s | any of api, postgres, redis, kafka, psp |
| reset | Toxiproxy `reset_peer`, request or response direction | same |
| blackhole | Toxiproxy `timeout` 0: bytes vanish, connections stay open | same |
| crash | `docker kill -s KILL`, restarted 1–5 s later | api, charger, sweeper, webhook processor, either relay, simulator |
| crash at a line | containers recreated with `FAILPOINTS=<name>=exit`: they die there on every pass, and restart, until disarmed; the summary counts the crashes | `charger.after_claim` and `charger.after_provider_call` (armed in charger and sweeper together: both run that code), `webhook_processor.before_commit` |

Three to six network faults, one to three kills and, half the time, a
crash point, over a provider profile drawn from the state machine's presets
(lost responses, declines, slow, webhook chaos, or none). The response
direction matters: a lost *response* to a write is exactly the case
idempotency exists for.

**The run** (`verification/faults.py`): two to four client tasks pay,
double-submit (the same request twice at once), refund, transfer and reuse
a key with a different body, while the timeline applies the plan. Every
request goes in the history with its time. A timeout, connection error,
5xx or 409 (same key still in flight) is *unknown* and is retried with the
same key and body, as a correct client would. Then every fault is removed,
every unknown request is retried until answered, the runner waits until no
work is left, reconciles every provider date, and checks.

**What it checks**: the same `checker.check_all` as the state machine (I1–I7,
I9, I10, the model, no unexplained reconciliation item), plus

- safety (I1 I2 I3 I5 I6, I9) **sampled every 3 s while faults are live**;
- **availability**: every request answered within 90 s of the faults
  being removed;
- **liveness**: no work left (outbox, `PENDING` or leased payments and
  refunds, unprocessed webhooks on either side) within 180 s.

On a violation the runner stops and leaves the stack running: history,
ground truth, plan and container logs go to `.verify-faults/<seed>/`, and the
database stays as the checker saw it (`make verify-faults-down` removes it).

**Its self-test** runs first: charger and sweeper restarted with the planted
bug (a fresh provider key per retry), every provider response delayed past
the charger's timeout and every webhook lost. The runner must report I3.
Losing the webhooks is necessary: they heal a lost response within
milliseconds, before any retry, which hides the bug (see the bug log).

**Reproducibility, honestly**: a seed fixes the plan (which faults, when,
which provider profile, how many clients), not the history. Real processes
and real time decide the interleaving, so a re-run of a failing seed applies
the same faults at the same seconds and may or may not hit the same race.
That is the trade: the state machine replays exactly but cannot see races;
this layer sees races but cannot replay them.

## Results

| Run | Histories | Steps checked | Provider charges | Violations |
|---|---|---|---|---|
| Nightly profile, developer's WSL2 with `CI=true`, 2026-11-22 (sweeper fix) | 361 | 9,460 | 1,140 (296 refunds) | 0 |
| Nightly profile, sandbox under CPU load, 2026-11-22 (deterministic machine) | 367 | 8,874 | 1,023 (287 refunds) | 0 |
| Nightly profile, developer's WSL2, 2026-11-22 (before the fix) | 384 | 8,808 | 1,050 | 0, then not replayable (see bug log) |
| Nightly profile, sandbox, 2026-11-21 | 400 | 9,026 | 1,614 (132 refunds) | 0 |
| PR profile | ~20 | ~450 | ~85 | 0 |

End states in that nightly run: 986 `SETTLED`, 130 `REFUNDED`, 498
`FAILED`, 54 `PROCESSING` (dead-lettered after exhausted retries). Redis was
down in 188 of the histories. The README quotes the nightly job's summary
(GitHub Actions → Nightly verification), not these numbers.

**Fault layer** (`make verify-faults`, 40 s scenarios):

| Run | Scenarios | Requests (unknown, retried) | Faults applied | Provider charges | Violations |
|---|---|---|---|---|---|
| Sandbox (2 CPUs), 2026-11-22, seeds 3000–3007 | 8 + self-test | 3,453 (140) | 36 network (13 blackhole, 10 latency, 9 reset, on all five proxies), 18 kills, 7 crashes at a failpoint | 1,591 for 1,591 payments (1,398 settled, 193 refunded) | 0 |

The self-test reported the planted double charge (I3) before every run.
A scenario takes about 50–65 s, quiet 4–17 s after its faults are removed.
Crash points crash 0–2 times per armed window: each crash costs a
container restart (Python start-up, a Kafka group rejoin) before the next
pass can reach the line.

## What is not checked (yet, or here)

- **Concurrency in the state machine.** The machine is sequential, and its
  workers process one item at a time (so that every history replays
  exactly). The fault layer runs the real concurrent system (concurrent
  sweeps, four webhook lanes, two relays, double submits), but cannot
  replay an interleaving it found.
- **Partitions between two services only.** Toxiproxy sits between each
  process and a dependency; it cannot split the network (for example, the
  relays from each other while both still reach Postgres). Clock skew is
  not injected.
- **Crash points in reconciliation.** `recon.after_line` is covered by the
  Stage 6 tests; the fault layer reconciles only after healing.
- **Transfers in the fault layer** are almost all refused (422): money
  becomes available only at settlement, and the fault layer settles once,
  after the run. They still check that a stored rejection replays; the
  state machine covers transfers that move money.
- **Settlement-file faults.** Covered by the Stage 6 tests; the machine
  asserts reconciliation finds nothing to explain.
- **Transfers are rare**: money must be settled before it can move, so only
  about 1 in 5 transfer attempts is acknowledged (29 in the nightly run).
- **I8** (every outbox row eventually published, each consumer applies each
  event once) is checked only indirectly, through the model's end states.
- Partial refunds, multiple currencies, clock skew between services.

## Bug log

Dated, newest first. Each entry: what failed, the minimal sequence, the fix,
the regression test.

### 2026-11-22 — Database outages answered 500, not 503 (found by the fault layer; fixed in Stage 8)

- **Found by:** every fault run's summary listed `create_payment 500`,
  `create_refund 500` and `create_transfer 500` (31 in one local run) while
  Postgres was being reset or blackholed. The client treated them as unknown
  and retried, so nothing was wrong afterwards, but the API was reporting an
  outage as a server bug.
- **Cause:** a lost or refused database connection escaped as an unhandled
  exception: `ConnectionResetError` while connecting, or SQLAlchemy's
  `DBAPIError` wrapping asyncpg's `ConnectionDoesNotExistError` mid-request.
- **Fix:** an exception handler answers 503 with `Retry-After: 1` and "retry
  with the same Idempotency-Key", only for connection failures
  (`app.core.db_errors.is_dependency_failure`); a broken query, a constraint
  or any other error is still a 500.
- **Regression tests:** `test_routers.py::test_a_dependency_outage_is_503_with_retry_after`
  and `test_a_query_error_is_still_a_500`; checked on the fault stack with a
  Postgres reset.

### 2026-11-22 — Fault layer: crash windows crashed nothing (found by counting the crashes)

- **Symptom**: every scenario with a crash point passed, and the summary's
  new crash counter (Docker's restart count for the armed containers) read
  0 across all of them. A manual test (arm, pay, wait 12 s) crashed the
  charger at once.
- **Causes, two**:
  1. Arming means replacing the container, and a graceful stop waits
     Docker's whole 10 s grace period (measured: 11.4 s, against 1.3 s
     with an immediate kill): the worker is PID 1 in its container, has no
     SIGTERM handler (`SigCgt` shows SIGINT only; uvicorn, for the API,
     catches SIGTERM), and the kernel ignores signals to PID 1 that it has
     no handler for. The window
     (2–10 s) was over before the armed containers were up.
  2. Armed in the charger alone, a charger crash point rarely met work: a
     replaced charger rejoins its Kafka group for seconds, and the sweeper
     claims the PENDING payment first, through the same code, unarmed.
- **Fix**: containers are replaced with `--timeout 0` (a kill: replacing
  an armed container is a crash anyway); a crash point is armed in every
  service whose code reaches it (charger and sweeper together); windows
  last 8–15 s. The summary reports crashes per scenario, so a window that
  crashes nothing is visible.
- **Regression tests**: `test_a_crash_point_is_armed_in_every_service_that_reaches_it`;
  the crash counter in every run's summary.
- **What it says about the system**: nothing wrong with correctness. But
  the PID 1 finding applies to every worker container in the dev and
  production setups: a stop or deploy always waits the full grace period
  and ends in SIGKILL. Correct (the system is crash-safe by design) but slow.
  **Fixed in Stage 8:** workers handle SIGTERM (`app/core/shutdown.py`),
  finish the item in hand and leave; a stop takes 0.3–1.4 s, exit 0
  (`tests/integration/test_shutdown.py`).

### 2026-11-22 — Fault layer: the self-test was masked by webhooks

- **Symptom**: the fault self-test (planted fresh-key bug, every provider
  response delayed past the charger's timeout) passed clean: no double
  charge.
- **Cause**: the provider's webhook reports the charge within milliseconds,
  and the webhook processor captures the payment long before the sweeper's
  first retry. With no retry there is nothing for the planted bug to break.
  The system's defence in depth hid the bug, the same effect as the planted
  webhook bug in the state machine's table.
- **Fix**: the self-test also drops every webhook (`webhooks_lost`), so only
  retries can learn a charge's outcome; it then reports I3 at once.
- **Regression test**: `test_the_self_test_loses_every_provider_response`;
  the self-test itself runs before every fault run.

### 2026-11-22 — Fault layer: the provider forgot its charges when killed (harness configuration)

- **Symptom**: the first scenario that killed the simulator failed with
  dozens of "payment is CAPTURED but the provider says no charge" and I7.
- **Cause**: the simulator's store was on tmpfs, which is wiped when a
  container stops. Killing the provider erased its ground truth. A real
  provider never forgets a charge; the checker was right to object.
- **Fix**: a named volume for the store (removed by `make verify-faults-down`).

### 2026-11-22 — Sweeper attempted claims in heap order, not due order (found by the nightly job)

- **Found by:** the first nightly run on GitHub, after the replayability fix
  below: `FlakyStrategyDefinition` again, after 196 clean histories. Not
  seen on the developer's WSL2 run (352 histories) or in the sandbox.
- **Cause, in the product:** the sweeper claimed rows with
  `UPDATE … WHERE id IN (SELECT … ORDER BY due LIMIT n) RETURNING …`.
  Postgres does not return `RETURNING` rows in the subquery's order but in
  physical heap order, which moves with HOT updates and vacuum timing. The
  sweeper then attempted the claims in that order: "oldest first" was not
  true, and in the harness (one claim at a time) the simulator's ordered
  fault draws met payments in a timing-dependent order.
- **Reproduced:** replaying GitHub's exact derandomized histories (`CI=true`)
  in the sandbox, the pushed code failed after 15 histories; with the fix,
  the same histories ran 367 clean.
- **Fix:** the claim is a CTE that locks the due rows oldest first, updates
  them, and returns them `ORDER BY due_at, created_at, id`; the same for
  refunds.
- **Regression test:**
  `test_charger.py::test_sweeper_hands_back_claims_oldest_due_first` lays
  the heap out in creation order with due order reversed; it fails 3 of 3 on
  the old query and passes on the new one.
- **Also found:** Hypothesis loads a derandomized, database-less `ci` profile
  whenever `CI` is set, and the nightly profile inherited it. Every night
  would have replayed the same 300 histories and discarded saved failures,
  making "five green nights" one run five times over. Profiles now name
  Hypothesis's `default` profile as parent; nightly is explicitly random
  with an example database
  (`test_harness.py::test_profiles_ignore_hypothesis_ci_profile`).
- This is the first bug in the system itself that the harness found.

### 2026-11-22 — Harness: histories were not replayable (FlakyStrategyDefinition)

- **Found by:** the developer's local `make verify` on WSL2, after 384
  histories (8,808 steps, 1,050 provider charges; no invariant violated).
  Hypothesis replayed a prefix of choices and the system did something
  different, so it stopped with `FlakyStrategyDefinition`. A history that
  cannot be replayed cannot be shrunk or reproduced, so this blocks the
  harness's main job.
- **Cause:** five things outside Hypothesis's control changed outcomes
  (whether a call timed out, a refund was accepted, a webhook was pending),
  which changed the bundles and enabled rules for the same choices:
  1. worker RNGs seeded from a run-wide counter (and shared between the
     charger and the webhook processor);
  2. the sweeper running claims concurrently: the simulator draws its fault
     decisions from one seeded stream in request order, and concurrent
     retries arrive in scheduler order, so a "lose this response" draw could
     land on a different payment;
  3. the webhook processor running four lanes concurrently;
  4. a 50 ms client timeout racing ordinary in-process calls under load;
  5. the sweeper comparing wall-clock `created_at` with the fake clock near
     its 60 s grace boundary, so the answer depended on machine speed.
- **Reproduced** in the sandbox only under CPU load (two busy loops):
  the old machine failed the same way at history 260; idle, it passed.
- **Fix:** fixed per-example seeds (the simulator's seed is now drawn by a
  rule, so it is part of the replayed history); sweeper and webhook
  processor run one item at a time in the harness; the fake clock runs an
  hour ahead of the database clock; and no wall clock decides a lost
  response: the simulator takes an injectable `delay`, which in the harness
  raises `TimeoutError` at the moment the response would go missing (after
  the charge is recorded), with the client's own timeout at 30 s.
- **First attempt failed, honestly recorded:** replacing the 50 ms timeout
  with 0.25 s still flaked under the same load (history 291): starved calls
  exceeded it too. Only taking the wall clock out entirely held: 367
  histories, 8,874 steps, under load, no flake.
- **Regression test:** `test_the_machine_is_deterministic` runs eight
  histories twice and requires identical outcomes. It guards the
  deterministic parts (seeds, ordering); timing races only show under load,
  so the nightly run remains the real check.
- **Cost:** the machine no longer exercises concurrent sweeps or concurrent
  webhook processing. Those races stay covered by the integration tests
  (Stages 4 and 5); a concurrency rule is backlog S7-2.

### 2026-11-21 — Harness: faults were cleared before in-flight work met them

- **Found by:** the self-test, which stopped catching the planted bug after
  an unrelated change to the rules.
- **Minimal sequence:** start with lost responses, create a payment, end the
  history. The quiesce step cleared the faults *before* any worker ran, so
  the payment was charged under a healthy provider and the planted retry
  never happened.
- **Fix:** quiesce runs one pass of every worker under the current faults,
  then lets the provider recover.
- **Regression test:** `test_harness_catches_a_planted_bug` (now finds it
  in 12–18 histories in both profiles).

### 2026-11-21 — Harness: Redis always up hid the Postgres replay path

- **Found by:** a planted bug (no fingerprint check on the database path)
  that the harness did not catch.
- **Cause:** the cache compares fingerprints first, so with Redis always up
  the database path never answered a retry.
- **Fix:** each history draws Redis up or down for its whole run.
- **Regression test:** the PR profile; the planted bug now fails with I9.

### 2026-11-21 — Harness: one fixed worker order hid retry bugs

- **Found by:** the self-test failing to catch the planted bug.
- **Cause:** quiesce always delivered webhooks before the sweeper retried.
  A webhook heals a lost response, so the retry that would charge twice
  never ran. Both orders happen in production.
- **Fix:** the quiesce rule draws the order; the end of every history
  sweeps first.

### Bugs in the system found by the harness

One: the sweeper attempted claims in heap order, not due order (2026-11-22,
above). It broke no money invariant (every order is safe), but "oldest
first" was false and made outcomes depend on vacuum timing. No history has
violated an invariant in the system itself.
Before the harness, the stage tests found one real bug (Stage 3: a payment
without a wallet would have been charged, failed to record and been charged
again on retry; fixed by skipping legacy payments before the provider call).
