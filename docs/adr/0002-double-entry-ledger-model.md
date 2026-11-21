# 0002. Model money as an append-only double-entry ledger in Postgres

- **Status:** Proposed
- **Date:** 2026-11-21
- **Stage:** 2

## Context

v1 has no money model. A payment row has an `amount` (`Numeric(12,2)`, passed
through the API and service as a Python `float`) and a status. There is no
balance, no record of where money sits after capture, and no way to answer "do
the books agree with the provider?". The `version` column exists but nothing
uses it.

Real payment systems record every money movement as immutable, balanced
entries and detect errors with queries ("clearing accounts must return to
zero"). Reconciliation (Stage 6) and the invariants I1, I2, I5, I6 and I7
need such a model to check against.

## Decision

1. Three tables: `accounts`, `transfers`, `entries`.
   - A transfer is one money event. It is unique on `(tenant_id, kind,
     reference_type, reference_id)`, so posting the same event twice is a
     no-op that returns the existing transfer.
   - An entry is one debit or credit line with `amount > 0`.
2. Amounts are integer minor units (`BIGINT`) with a currency code; a transfer
   is single-currency. No floats or `Decimal` round-trips anywhere in the
   money path. `payments.amount` migrates to `BIGINT`.
3. Accounts follow the TigerBeetle model: `debits_posted` and
   `credits_posted` running totals, a `normal_side`, and a `guarded` flag.
   Guarded accounts (wallet pending and available) have a CHECK
   `debits_posted <= credits_posted`, so they cannot go negative.
4. The database enforces the model, not only the application:
   - a deferred constraint trigger checks at commit that each transfer's
     debits equal its credits, per currency;
   - triggers reject UPDATE, DELETE and TRUNCATE on `entries` and
     `transfers`; corrections are new reversing transfers;
   - the application connects as a role that lacks the privileges to bypass
     this. Migrations run as the owner, because revoking privileges has no
     effect on a table's owner.
5. Account kinds per tenant: `psp_clearing`, `bank`, `psp_fees`,
   `refunds_payable`; per wallet: `wallet_pending`, `wallet_available`.
   Posting rules are the table in DESIGN.md section 6.3.
6. The ledger is updated synchronously, in the same transaction as the state
   change that causes it, never by an asynchronous consumer.

## Alternatives considered

| Option | Why not |
|---|---|
| Mutable `balance` column with optimistic locking | Balances cannot be audited or recomputed. A lost update or a bug leaves no trail. |
| TigerBeetle | Purpose-built and fast, but a second database to run and keep consistent with Postgres. See ADR on TigerBeetle (Stage 8). |
| Formance / pgledger as a dependency | Hides the modelling this project exists to show. pgledger is a useful reference for the trigger approach. |
| Signed amounts in one column | Sign mistakes are a classic ledger bug; explicit direction plus a normal side avoids them. |

## Consequences

- "Are balances right?" becomes a set of SQL queries (I1, I2, I5, I6, I7).
- Fixtures cannot clean ledger tables as the application role; they must use
  the owner role. That friction is the proof the protection works.
- System accounts (`psp_clearing`, `bank`, `psp_fees`) are hot rows. They cap
  worker throughput per tenant; the ceiling is measured in Stage 8.
- Partial refunds and FX are out of scope; one currency per transfer.

## What would change my mind

- Measured transfer throughput where hot-row contention on system accounts,
  not the API, is the bottleneck and per-account sharding is not enough.
- A requirement for multi-currency transfers (would need FX accounts and a
  rate source, a different design).
- Needing ledger writes from systems that cannot share this database.
