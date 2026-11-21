# 0008. Reconcile each provider file date in per-line transactions; report, never correct

- **Status:** Accepted
- **Date:** 2026-11-21
- **Stage:** 6

## Context

The charger, webhooks and the sweeper can all miss an outcome: a response
is lost, the webhook is dropped, and the sweeper's retries run out (the
payment stays `PROCESSING` and goes to the DLQ, ADR 0006). The provider can
also be wrong: its file lists a charge twice, with the wrong amount, or not
at all. Until Stage 6 nothing compared our books with the provider's, and
captured money sat in `psp_clearing` forever, because nothing posted
settlement.

## Decision

1. **A run reconciles one provider file date, platform-wide.** There is one
   provider account for every tenant, so a file mixes tenants; each line is
   resolved to a tenant through its payment. `recon_runs` has no tenant
   (the guide's table did); items carry the tenant. Only platform API keys
   may run or read reconciliations.
2. **One transaction per line**, in `line_id` order. The line locks its
   payment (then refund, then accounts, the global order), classifies, posts
   and records its item together.
3. **Match on provider reference, then merchant reference; never amount.**
4. **Only `matched` and `healed` post.** `amount_mismatch`,
   `missing_in_ledger`, `duplicate_line`, `missing_at_psp` and
   `status_conflict` are reported and left alone.
5. **Heal only what the provider proves and we left undecided.** A line for
   a `PROCESSING` payment (or refund) applies the outcome through the same
   entry point as every other source, then matches. A line for a `FAILED`
   payment is a `status_conflict`, not a heal.
6. **Record the settling line on the payment or refund**, once
   (trigger-guarded). That is what separates "re-run of the same line"
   (post nothing, `matched`) from "a second line for the same charge"
   (`duplicate_line`).
7. **A session advisory lock per (provider, date)** keeps two runs of one
   date from interleaving. A run found `running` under the lock is marked
   `abandoned`.
8. **Missing at the provider is checked once per capture date**: the run for
   date D reports captures from `D - RECON_SETTLEMENT_WINDOW_DAYS` that no
   loaded line mentions.
9. **The API runs synchronously**; large files belong to the CLI job.

## Alternatives considered

| Option | Why not |
|---|---|
| One transaction for the whole file | A crash or one bad line rolls back hours of work, and the locks are held for the whole file. |
| Match by amount and date when references are missing | Two equal payments the same day become interchangeable; a wrong match posts real money to the wrong merchant. |
| Auto-correct amount mismatches (post the provider's amount) | Hides the cause; the ledger would then agree with a provider bug. |
| Freeze each day's file on first download in the simulator | Mid-day downloads would miss later charges; deterministic files keyed by reference give stable line ids without freezing. |
| Async API with a background task | A run lost on API restart needs its own recovery; synchronous is enough at this scale, and the CLI exists for big files. |
| Per-tenant runs | The provider file is per account, not per tenant; unknown lines would belong to no run. |

## Consequences

- Healed payments end `SETTLED` without the sweeper or a webhook (tested,
  and on the running stack: 15 of 30 payments stuck `PROCESSING` with
  webhooks dropped and the sweeper off, all healed and settled by one run).
- `psp_clearing` returns to zero after a clean day (I7, tested).
- Re-runs and crashes are safe by construction (tested: a second run posts
  0; a crash after the first line resumes and posts only the rest).
- Metrics live in the process that ran the reconciliation; CLI runs alert by
  exit code. A scheduler and durable metrics are Stage 8 work.
- Unexplained items need a human; there is no resolution workflow yet.

## What would change my mind

- Files large enough that per-line transactions are too slow (batch by
  payment hash, still one payment per transaction).
- More than one provider, or per-tenant provider accounts (then runs are
  per account).
- A provider whose references are not stable across files (then matching
  needs a different key, and this ADR is revisited).
