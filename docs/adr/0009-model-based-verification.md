# 0009. Verify with an in-process model-based state machine checked against the provider's ground truth

- **Status:** Accepted
- **Date:** 2026-11-21
- **Stage:** 7

## Context

Every stage so far has example-based tests: the cases someone thought of.
The failures this system exists to survive are combinations: a lost response,
then a webhook dropped, then a retry, then a refund, then reconciliation.
Nobody writes those by hand in every order. We need generated histories, a
definition of "correct" that does not depend on knowing which order ran, and
proof that the harness can find a bug at all.

## Decision

1. **A Hypothesis `RuleBasedStateMachine`** over the whole system, run
   in-process: the API through the ASGI transport, the simulator the same
   way, workers through their single steps, the real database. Kafka is a
   queue fed by the real outbox publisher.
2. **Two kinds of checks.** Safety invariants (I1, I2, I3, I5, I6) and
   replay identity (I9) after every step; completeness (I4, I7), I10 and the
   model after a quiesce step that drains every worker and reconciles.
3. **The model tracks what is allowed, not what is expected.** It is built
   from the client's acknowledged history and, once quiet, consults the
   provider's ground truth to derive each row's end state and every wallet
   balance to the minor unit.
4. **One checker for every source of histories.** The machine's history and
   the fault-layer scenarios' JSONL files go through `verification.checker`.
5. **A planted bug is part of the suite** (`app/core/planted_bugs.py`): the
   harness must fail with it on. It cannot be enabled in production.
6. **Profiles:** `pr` (~20 derandomized histories) in every CI build;
   `nightly` (300 random histories) on a schedule, with Hypothesis's example
   database cached so past failures replay first.

## Alternatives considered

| Option | Why not |
|---|---|
| Full-stack randomized runs only (containers, Kafka, real time) | Slow, timing-dependent, hard to shrink; a failure is a 20-minute log, not a five-step reproduction. Kept for the fault layer (part 2), where real networks matter. |
| Predict one expected state per step | Wrong for an asynchronous system: the model would encode one interleaving and reject correct ones. |
| Check only the ledger invariants | They cannot see a payment charged but never captured, or captured and never settled; only ground truth plus the client's history can. |
| Property tests per function | Already exist (Stage 2); they cannot reach cross-worker interleavings. |

## Consequences

- 400 generated histories (9,026 steps, 1,614 provider charges) ran clean;
  three weaknesses in the harness itself were found by the self-test and
  fixed (docs/verification.md, bug log).
- The machine is sequential: concurrency races stay with the integration
  tests. Network faults and process kills arrive with part 2.
- A planted bug the invariants cannot see (a dropped webhook healed by the
  sweeper) is documented, not hidden: defence in depth masks it.
- **Every history must replay exactly** (Hypothesis rejects a run whose
  same choices behave differently). So nothing outside Hypothesis's choices
  may decide an outcome: seeds are fixed or drawn, harness workers run one
  item at a time, and lost responses are injected, not timed. Learned the
  hard way (docs/verification.md, 2026-11-22).
- PR CI gains ~40 s; the nightly run takes ~6–11 minutes on a laptop.

## What would change my mind

- A real bug found in production or by hand that the harness could have
  generated but did not: then the rules or the model are missing something.
- Runs where shrinking takes longer than the bug is worth: then cap
  shrinking in the nightly profile and keep the raw seed.
