# Verification

"Tests pass" says the cases someone thought of work. This harness says
something stronger: **these invariants held across N generated adversarial
histories**, and it proves it can tell, by catching a bug planted on purpose.

Code: `verification/` (library), `tests/verification/` (entry points).
Decision record: ADR 0009.

## Running it

```bash
make verify-quick    # ~20 derandomized histories + self-test (~30 s); runs in every CI build
make verify          # 300 random histories + self-test (~6 min); the nightly job
VERIFY_EXAMPLES=1000 make verify
```

Both need Postgres and Redis, like `make test`. `make verify` writes the
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

The same checker runs offline on a recorded history
(`python -m verification.checker --history … --ground-truth …`); Stage 7
part 2's fault-layer scenarios use it that way.

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

## Results

| Run | Histories | Steps checked | Provider charges | Violations |
|---|---|---|---|---|
| Nightly profile, sandbox under CPU load, 2026-11-22 (deterministic machine) | 367 | 8,874 | 1,023 (287 refunds) | 0 |
| Nightly profile, developer's WSL2, 2026-11-22 (before the fix) | 384 | 8,808 | 1,050 | 0, then not replayable (see bug log) |
| Nightly profile, sandbox, 2026-11-21 | 400 | 9,026 | 1,614 (132 refunds) | 0 |
| PR profile | ~20 | ~450 | ~85 | 0 |

End states in that nightly run: 986 `SETTLED`, 130 `REFUNDED`, 498
`FAILED`, 54 `PROCESSING` (dead-lettered after exhausted retries). Redis was
down in 188 of the histories. The README quotes the nightly job's summary
(GitHub Actions → Nightly verification), not these numbers.

## What is not checked (yet, or here)

- **Concurrency.** The machine is sequential, and its workers process one
  item at a time (so that every history replays exactly). Races
  (two requests with one key at once, processor against charger, two relay
  replicas) are covered by the integration tests of Stages 1, 3 and 5.
- **Kafka, real networks, process crashes.** Stage 7 part 2 adds Toxiproxy
  (latency, reset, blackhole) and process kills, checked by the same
  checker on recorded histories.
- **Crashes inside a step.** Failpoints are tested per stage (charger,
  webhook processor, reconciliation), not generated by the machine.
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

None so far: no history has violated an invariant in the system itself.
Before the harness, the stage tests found one real bug (Stage 3: a payment
without a wallet would have been charged, failed to record and been charged
again on retry; fixed by skipping legacy payments before the provider call).
