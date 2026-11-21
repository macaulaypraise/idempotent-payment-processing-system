# 0003. Update account totals with atomic in-place increments in sorted order

- **Status:** Accepted (implemented in Stage 2)
- **Date:** 2026-11-21
- **Stage:** 2

## Context

A transfer touches two or more accounts, and some accounts (`psp_clearing`,
`bank`, `psp_fees`) are touched by almost every transfer. v1's only
concurrency tool is a `version` column on payments, intended for optimistic
locking but never used.

Two transfers moving money in opposite directions between the same two
wallets will deadlock if each locks its own source account first. Optimistic
concurrency on hot rows turns contention into retry storms.

## Decision

1. `post_transfer` updates each account with one statement of the form
   `UPDATE accounts SET debits_posted = debits_posted + :d,
   credits_posted = credits_posted + :c WHERE id = :id`. It never reads a
   balance into Python, computes, and writes it back.
2. Accounts are updated in ascending `id` order within a transaction. Across
   the codebase the lock order is always: payment row first, then accounts
   in ascending id.
3. Overdraft protection comes from the database CHECK on guarded accounts
   (ADR 0002). A violation surfaces as an integrity error, which the service
   translates to `InsufficientFundsError`; under the idempotency flow that is
   stored as a 422 and replayed on retry.
4. Drop the unused `payments.version` column.

## Alternatives considered

| Option | Why not |
|---|---|
| Optimistic version column per account | Every concurrent transfer on a hot account conflicts and retries; throughput collapses exactly where it matters. |
| `SERIALIZABLE` isolation | Correct, but serialization failures on hot accounts need the same retry loop, plus broader predicate locking. |
| `SELECT … FOR UPDATE` then compute in Python | Works with sorted locking, but adds a round trip per account and invites read-modify-write bugs. |
| Derive balances from `SUM(entries)` on every read | Always correct, but O(entries) per read and no place for an overdraft guard. Kept as the *check* (I5), not the mechanism. |

## Consequences

- Concurrent opposing transfers serialize instead of deadlocking. The Stage 2
  exit gate tests this with 1,000 repetitions.
- Totals are a cache of the entries; invariant I5 checks they agree.
- One function that locks in a different order is enough to reintroduce
  deadlocks, so lock order is a review checklist item.

## Results (Stage 2)

- 1,000 pairs of opposing transfers (A→B and B→A, 100 pairs at a time,
  2,000 transfers) finished with 0 deadlocks and both balances unchanged.
- Planted bug: locking accounts in line order instead of sorted order makes
  the same test fail with deadlocks, so the test can detect the problem.
- 200 concurrent 1-unit withdrawals from a wallet holding 10: exactly 10
  succeed, 190 get `InsufficientFundsError`, the balance never goes negative.

## What would change my mind

- Deadlocks observed in the concurrency test or under Locust despite sorted
  order (would mean another code path breaks the order, or a trigger takes
  locks unexpectedly).
- Hot-row wait time on system accounts dominating latency; then stop caching
  totals on system accounts and derive them from entries with periodic
  snapshots (DESIGN section 12).
