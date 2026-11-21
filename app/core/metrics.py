from prometheus_client import Counter, Gauge

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
