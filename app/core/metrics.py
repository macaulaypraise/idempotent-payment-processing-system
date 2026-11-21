from prometheus_client import Counter, Gauge, Histogram

# payments_created_total tracks every payment creation outcome
payments_created_total = Counter(
    "payments_created_total",
    "Total number of payment creation attempts",
    # success: new payment; idempotent_hit: replayed from Postgres;
    # cache_hit: replayed from Redis; mismatch: 422; conflict: 409 lock timeout;
    # rejected: deterministic business rejection stored under the key
    ["status"],
)

# Redis is optional; count how often it fails so a silent outage is visible
cache_errors_total = Counter(
    "idempotency_cache_errors_total",
    "Redis errors on the idempotency cache path (request continues without cache)",
    ["operation"],
)

idempotency_keys_expired_total = Counter(
    "idempotency_keys_expired_total", "Idempotency key rows deleted after expiry"
)

# outbox_lag_seconds tracks age of oldest unpublished outbox event
outbox_lag_seconds = Gauge(
    "outbox_lag_seconds", "Age in seconds of the oldest unpublished outbox event"
)

# dlq_depth tracks how many events are in the dead letter queue
dlq_depth = Gauge("dlq_depth", "Number of events currently in the dead letter queue")

# Every idempotent write route: route = "payments" | "wallets" | "transfers";
# outcome = created | replayed_db | replayed_cache | rejected | mismatch | conflict
idempotent_requests_total = Counter(
    "idempotent_requests_total",
    "Idempotent write requests by route and outcome",
    ["route", "outcome"],
)

ledger_transfers_total = Counter(
    "ledger_transfers_total", "Ledger transfers posted", ["kind"]
)

# Provider outcomes by who reported them and whether they changed anything
psp_results_total = Counter(
    "psp_results_total",
    "Provider outcomes applied to payments and refunds",
    ["source", "outcome", "result"],
)

# Charger attempts: kind = payment | refund; result = captured | declined |
# refunded | refund_failed | retry_scheduled | dead_lettered | conflict
charger_attempts_total = Counter(
    "charger_attempts_total", "Provider calls made by the charger", ["kind", "result"]
)

# Webhook ingestion: stored | duplicate | bad_signature | malformed | too_large
webhooks_received_total = Counter(
    "webhooks_received_total", "Provider webhook requests by result", ["result"]
)

# Webhook processing: processed | already_applied | parked | dead | ignored
webhooks_processed_total = Counter(
    "webhooks_processed_total",
    "Stored webhook events handled by the processor",
    ["event_type", "result"],
)

# created_at -> published_at for every outbox event the relay publishes
outbox_publish_lag_seconds = Histogram(
    "outbox_publish_lag_seconds",
    "Seconds from outbox insert to Kafka acknowledgement",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

outbox_leader = Gauge("outbox_leader", "1 while this relay holds the outbox lock")

# Reconciliation (Stage 6). Set by the process that ran the reconciliation.
recon_items_total = Counter(
    "recon_items_total", "Reconciliation items by classification", ["classification"]
)
recon_unexplained_items = Gauge(
    "recon_unexplained_items",
    "Unexplained items in the last completed reconciliation run",
)
recon_last_success_timestamp = Gauge(
    "recon_last_success_timestamp_seconds",
    "Unix time the last reconciliation run completed",
)
