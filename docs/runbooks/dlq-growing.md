# Runbook: dead letters growing

Something was given up on and needs a human or reconciliation. Three
sources write `dlq.v1` events:

| Source | Means | Row left as |
|---|---|---|
| charger / sweeper | provider calls failed `PSP_MAX_ATTEMPTS` (6) times with no answer | payment or refund `PROCESSING`, `next_attempt_at` NULL |
| webhook processor, after `WEBHOOK_MAX_ATTEMPTS` | an event could never be applied (its payment never became ready) | `webhook_events.status = 'dead'` |
| webhook processor, at once | an event contradicts what is recorded (a decline for a captured payment), is malformed, or names a payment we do not know | `webhook_events.status = 'dead'` |

A dead-lettered payment is not lost: the next reconciliation either matches
it to a settlement line (the charge happened: it is `healed` and captured) or
reports it.

## Symptom

- `make dlq` counts growing day over day.
- The `dlq.v1` topic's end offset growing (Kafka UI, http://localhost:8080).
- `charger_dead_lettered` or `webhook_dead_lettered` lines in worker logs.

(The workers' own metrics, such as `dlq_depth`, are not scraped yet:
backlog S8-1. Use the database, which is the truth anyway.)

## Confirm

```bash
make dlq          # dead-lettered payments, refunds, webhooks; dlq.v1 events in 24 h
docker compose logs --since 1h charger sweeper webhook_processor | grep -E 'dead_lettered|dlq'
```

## Immediate action

1. **Is the provider down?** Many payment dead letters at once, all with
   timeouts or 5xx, means an outage. Check `curl -s localhost:8001/health`
   and the provider's status page. Nothing is charged twice in the meantime:
   every retry used the same provider key.
2. **Let reconciliation decide payments.** Run the date
   (`make recon DATE=YYYY-MM-DD`). Charges the provider did make are
   `healed`; the rest stay `PROCESSING` and the provider has no charge.
3. **Dead webhooks:** read `webhook_events.last_error` before doing anything
   (`make dlq` prints the latest).
   - `conflicting outcome: …`: an incident. Our record and the provider's
     disagree about an outcome. Find which side is wrong before touching
     money.
   - `gave up: …`: the event never became applicable, usually a payment
     created outside this system or before v2 (no wallet). Check the id.
   - anything else (malformed, not one of our ids): raise with the provider.
4. Never re-drive a payment by hand by setting its status. Statuses move only
   through `apply_psp_result`; the database rejects invalid transitions.

## Root-cause checks

- Provider latency above `PSP_TIMEOUT_SECONDS` (3 s): every call times out,
  retries exhaust. The simulator's `slow` fault reproduces it.
- Webhook secret mismatch between the provider and `PSP_WEBHOOK_SECRET`:
  webhooks are rejected at the door (`webhooks_received_total{result="bad_signature"}`),
  which shows up as payments healed only by retries and reconciliation.
- Clock skew beyond `PSP_WEBHOOK_TOLERANCE_SECONDS` (300 s): same symptom.
