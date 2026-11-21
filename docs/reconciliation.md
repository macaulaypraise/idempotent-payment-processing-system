# Reconciliation

Every day the provider publishes a settlement file: one line per charge and
refund it settled, with gross, fee and net. Reconciliation answers one
question: **do the provider and our books agree?** Every disagreement is
detected, classified and reported. Nothing is corrected automatically.

Design: DESIGN 6.6. Decision record: ADR 0008. Code:
`app/services/reconciliation.py`.

## Running it

```bash
make recon DATE=2026-11-21                      # CLI; exit 0 clean, 2 unexplained, 1 failed
python -m app.workers.recon_job --date 2026-11-20 --date 2026-11-21   # several, in order
curl -X POST localhost:8000/v1/reconciliations -H "Authorization: Bearer $PLATFORM_KEY" \
     -H 'Content-Type: application/json' -d '{"date":"2026-11-21"}'
curl "localhost:8000/v1/reconciliations/$RUN_ID?unexplained_only=true" -H "Authorization: Bearer $PLATFORM_KEY"
```

Reconciliation settles every tenant's money through the platform's single
provider account, so only **platform API keys** (keys of the default tenant,
`make api-key TENANT=default`) may run or read it. A merchant's key gets 403.

Dates are UTC. Reconcile dates **in order**: the missing-at-provider check
for a date looks at every file loaded so far.

## The file

```
line_id,type,psp_reference,merchant_reference,gross,fee,net,currency
ch_1a2b…,charge,ch_1a2b…,<payment id>,1000,59,941,USD
re_9f8e…,refund,re_9f8e…,<refund id>,1000,0,1000,USD
```

- Amounts are integer minor units. For a charge, `net = gross - fee`; for a
  refund, the fee is 0 and the money goes back.
- `merchant_reference` is our payment id (charges) or refund id (refunds).
- Loading is idempotent: `(provider, file_date, line_id)` is unique. A line
  already loaded is kept as first loaded, even if a later download differs.
- A malformed file (missing column, unknown type, non-integer amount) fails
  the run and loads nothing.

## Matching

For each line, in `line_id` order, in its own transaction:

1. **Find the row.** First by provider reference (`payments.psp_reference`
   or `refunds.psp_reference`), then by merchant reference (our id). Never
   by amount: two payments of 10.00 on the same day are not the same payment.
2. **Lock it** (payment, then refund, then accounts, the same order as
   everywhere else), so a webhook or the charger cannot change it meanwhile.
3. **Classify** (first rule that applies):

| Classification | When | Posts | Explained |
|---|---|---|---|
| `missing_in_ledger` | no payment or refund matches either reference | nothing | no |
| `status_conflict` | pre-ledger payment; our recorded provider reference differs from the line's; the provider settled a charge we recorded as `FAILED` (or still `PENDING`), or a refund we recorded as `FAILED`/`PENDING` | nothing | no |
| `amount_mismatch` | currency differs, gross differs from our amount, `net != gross - fee`, charge fee outside `[0, gross)`, or a refund line with a fee | nothing | no |
| `matched` (re-run) | this same line already settled the row | nothing | yes |
| `duplicate_line` | a different line already settled the row | nothing | no |
| `healed` | the row was stuck `PROCESSING` (response lost, webhook dropped): the outcome is applied through `apply_psp_result` / `apply_refund_result`, then the line is matched | as `matched`, plus the capture or refund confirmation | yes |
| `matched` | everything agrees | see below | yes |

After the lines, one more check:

| Classification | When | Posts | Explained |
|---|---|---|---|
| `missing_at_psp` | a capture (or confirmed refund) dated `file date - RECON_SETTLEMENT_WINDOW_DAYS` that no loaded line mentions | nothing | no |

The window (default 1 day) allows for the provider's cut-off: a charge made
just before midnight may appear in the next day's file. Each capture date is
checked by exactly one date's run (re-runs repeat the same finding), so the
report never re-lists last month's captures every day.

## What a match posts

| Line | Transfer | Debit | Credit |
|---|---|---|---|
| charge | `settlement_charge` | `bank` (net), `psp_fees` (fee) | `psp_clearing` (gross) |
| charge | `release`, only if the payment is `CAPTURED` with no active refund | `wallet_pending` | `wallet_available` |
| refund | `settlement_refund` | `psp_clearing` | `bank` |

Then a `CAPTURED` payment becomes `SETTLED` (a `REFUNDED` one stays
`REFUNDED`), `settled_at` is set, and the line is recorded in
`settlement_line_id`. A trigger refuses to change that column once set.

Why the release is skipped while a refund is active: the refund hold has
already moved the money from pending into `refunds_payable`. Releasing as
well would pay the merchant twice. If that refund later fails, its hold
returns to `wallet_available`, because the payment is `SETTLED` by then.

## Guarantees

- **Re-running a date posts nothing.** Every transfer is unique per payment
  or refund. The settling line is recorded once, so the re-run finds "already
  settled by this line" and reports `matched` with nothing posted.
- **A crash mid-file loses nothing.** Each line commits on its own. The next
  run of the date marks the crashed run `abandoned` and processes every line
  again; the ones already settled post nothing.
- **Two runs of one date cannot interleave.** A run holds a session advisory
  lock for `(provider, date)`; a second gets `ReconInProgressError` (409 from
  the API). Without the lock, the order in which duplicates are seen, and so
  which line is called the duplicate, would depend on timing.
- **I7.** Once every file is reconciled and no refund is in flight,
  `psp_clearing` and `refunds_payable` are zero for every tenant
  (`verification.invariants.i7_clearing_accounts_settled`).

## What to do with each unexplained item

| Classification | Likely cause | Action |
|---|---|---|
| `amount_mismatch` | provider error, or our amount is wrong | compare with the provider's dashboard; raise with the provider; correct by a reviewed manual entry |
| `missing_in_ledger` | a charge made outside this system, or a provider error | find who made it; refund at the provider if it should not exist |
| `duplicate_line` | provider listed one charge twice | raise with the provider; the bank received money once or twice? Check the payout |
| `missing_at_psp` | provider has not settled yet, dropped it, or our capture is wrong | wait one more window; then ask the provider; look up the charge (`GET /charges/{id}`) |
| `status_conflict` | our record and the provider's disagree about the outcome | treat as an incident: find which side is wrong before touching money |

The `resolved` column on items is for this workflow; there is no endpoint to
set it yet (backlog S6-1).

## Metrics and alerts

- `recon_items_total{classification}`, `recon_unexplained_items`,
  `recon_last_success_timestamp_seconds`: set by the process that ran the
  reconciliation. Runs through the API show up in the API's `/metrics`. CLI
  runs report through the exit code (2 = unexplained items).
- Alerts (`prometheus/alerts.yml`): `ReconciliationUnexplainedItems`
  (unexplained > 0) and `ReconciliationStale` (no completed run for 36 h).

## What this does not do

- No automatic correction of anything flagged. Correcting automatically
  would hide the bug that caused the difference.
- No partial refunds, fees on refunds, chargebacks, FX or multi-provider
  files.
- No check of the bank statement against `bank` (the next reconciliation a
  real system would add).
