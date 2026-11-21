# 0007. Ingest provider webhooks into a Postgres inbox; apply them asynchronously

- **Status:** Accepted
- **Date:** 2026-11-21
- **Stage:** 5

## Context

v1's `POST /v1/webhooks` accepted any JSON body: no signature, no storage, no
deduplication, and no effect (D10). Real providers send webhooks at least
once, retry on any non-2xx or timeout, reorder them, and can send them before
our own transaction has committed. A webhook is also the only way to learn an
outcome when the provider's synchronous response was lost.

Since Stage 3 every outcome goes through one idempotent entry point,
`apply_psp_result` / `apply_refund_result`, which locks the payment, ignores
an outcome already reflected, refuses contradictions and parks early ones.

## Decision

1. **Verify, store, acknowledge.** `POST /v1/webhooks/psp` checks an
   HMAC-SHA256 signature over `"{timestamp}.{raw body}"`
   (`PSP-Signature: t=…,v1=…`, constant-time compare, ±300 s tolerance from
   the injected clock, several `v1` values accepted for secret rotation, an
   empty secret rejects everything). It then inserts the event into
   `webhook_events` with `ON CONFLICT (provider, provider_event_id) DO
   NOTHING` and returns 200 for new and duplicate events alike. A bad
   signature or malformed event is a 400 and nothing is stored. No business
   logic runs in the request.
2. **Process asynchronously, one transaction per event.** The processor
   claims a due row with `FOR UPDATE SKIP LOCKED`, resolves the tenant from
   the payment or refund (`merchant_reference` is our id; the provider does
   not know tenants), applies the outcome through the same entry point as the
   charger, and records the row's status in the same transaction.
3. **Outcomes.** Applied or already applied → `processed`. Unknown id or not
   applicable yet → `parked` with exponential backoff, then `dead` + a
   `dlq.v1` event after `WEBHOOK_MAX_ATTEMPTS`. Contradiction → `dead` +
   `dlq.v1` at once; never applied automatically. Malformed or pre-ledger →
   `dead` + `dlq.v1`. Unknown event type → `ignored`.
4. **The signing code is shared** with the simulator (`sign_webhook`), and
   pinned by a unit test against `hmac` directly so a shared bug cannot pass.

## About the guide's "refund webhook before charge webhook" gate

The build guide's gate reads: a refund-succeeded webhook delivered before its
charge-succeeded webhook is parked and later applied. Two different things
hide in that sentence, and only one can happen here:

- **Delivery order: can happen, and is tested.** The charger records both
  the capture and the refund from the provider's responses; the provider
  then delivers `refund.succeeded` before a delayed `charge.succeeded`. Both
  webhooks find their outcome already applied (a capture reported for a
  `REFUNDED` payment counts as already applied). A permutation test delivers
  {charge, refund, charge again, refund again} in all 6 distinct orders and
  checks the final state is identical every time, with nothing dead-lettered.
- **Applicability order: cannot happen.** A refund cannot be requested until
  the capture is recorded, and the charger commits `PROCESSING` before it
  calls the provider. So a refund webhook never arrives for a refund that
  does not exist yet because its charge is unrecorded.

What does need parking is an event that genuinely cannot apply yet: an id
not visible yet (the provider was faster than our commit), or a payment not
yet `PROCESSING`. Both are parked and tested; the second is applied once the
payment is claimed. The unknown-id case gives up into the DLQ.

## Alternatives considered

| Option | Why not |
|---|---|
| Apply in the request | Holds the provider's connection on our locks and ledger work; a slow or failing apply turns into provider retries and duplicate load. |
| Deduplicate in Redis | Puts Redis on a correctness path; the unique constraint is exact and transactional. |
| Drop events that do not apply yet | Loses the outcome when the provider is faster than our commit. |
| Per-tenant webhook secrets | The provider is one party for the platform here; revisit if tenants bring their own provider accounts. |

## Consequences

- Webhooks heal lost responses without the sweeper (tested in process and
  on the running stack: 300 payments, 30% of responses lost, sweeper off,
  every payment `CAPTURED` with one charge).
- The charger and the processor can report the same outcome at the same
  moment; the payment row lock serialises them (race test: 20 payments, one
  capture transfer each).
- A crash between apply and commit loses nothing (failpoint test).
- One more worker to run, and the inbox grows: add retention for
  `processed`/`ignored` rows (Stage 8).
- The provider's signature timestamp must be close to our clock; the
  tolerance is configurable.

## What would change my mind

- Inbox processing lag that matters for user-visible state (then add NOTIFY
  to the inbox like the outbox).
- A provider that signs differently or per-account secrets.
- Webhook volume where one row per transaction is the bottleneck.
