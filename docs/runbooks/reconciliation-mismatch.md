# Runbook: reconciliation mismatch

The provider's settlement file and the ledger disagree. Reconciliation
reports and never corrects (ADR 0008): correcting automatically would hide
the bug that caused the difference.

## Symptom

- Alert `ReconciliationUnexplainedItems` (`recon_unexplained_items > 0`).
- `make recon DATE=...` exits 2, or `POST /v1/reconciliations` reports
  `"unexplained": N`.
- Alert `ReconciliationStale`: no run completed in 36 h (a different
  problem: the job is not running, or the file cannot be fetched, 502).

## Confirm

```bash
make recon DATE=2026-11-22        # re-running a date is safe: nothing posts twice
curl -s -H "Authorization: Bearer $PLATFORM_KEY" \
  "localhost:8000/v1/reconciliations/$RUN_ID?unexplained_only=true" | jq '.items'
```

Only platform (default-tenant) keys can read runs (`make api-key TENANT=default`).
Each item has a classification, the payment or refund, the expected and the
actual amount, and the settlement line.

## Immediate action, by classification

| Classification | Likely cause | Action |
|---|---|---|
| `amount_mismatch` | provider error, or our amount is wrong | Compare with the provider's dashboard. Our amount is immutable (a trigger refuses changes), so a wrong amount on our side is a bug at creation. Raise with the provider. |
| `missing_at_psp` | not settled yet, dropped by the provider, or a capture that should not exist | Wait one window (`RECON_SETTLEMENT_WINDOW_DAYS`); then look the charge up (`GET /charges?merchant_reference=<payment id>` on the provider). |
| `missing_in_ledger` | a charge made outside this system, or a provider error | Find who made it. Refund at the provider if it should not exist. |
| `duplicate_line` | the provider listed one charge twice | Check the payout: was the money received once or twice? |
| `status_conflict` | our record and the provider's disagree about the outcome | Incident. Find which side is wrong before touching money. |

**Re-running does not clear an item.** Settlement lines are stored once: a
corrected file for the same date does not overwrite the flagged line, so the
item stays until it is resolved. The `resolved` flag on items has no endpoint
yet (backlog S6-1); a correction is a reviewed manual ledger entry, not an
edit.

## What is still true while an item is open

- The flagged payment stays `CAPTURED`: its money remains in
  `psp_clearing` and in the wallet's pending balance, not available.
- Every other line of the file settled normally. I7 (`psp_clearing` = 0)
  holds again once the item is resolved.

## Root-cause checks

- Many `missing_at_psp` for one day: the provider's file is late or
  partial; check its line count against the day's captures.
- `amount_mismatch` on every line: fee or currency parsing changed in the
  file format (`app/services/reconciliation.py`, `parse_settlement_csv`).
- Items on payments created before v2 (no wallet): legacy rows, expected
  (the Stage 3 migration keeps them out of the ledger).

## Evidence

- `make demo` step 5 injects an amount mismatch: flagged, its payment left
  unsettled, every other line settled with its fee.
- `tests/integration/test_reconciliation.py`: each discrepancy type detected
  and classified; re-runs post nothing; crash mid-file resumes.
