from prometheus_client import Counter, Gauge

# payments_created_total tracks every payment creation outcome
payments_created_total = Counter(
    "payments_created_total",
    "Total number of payment creation attempts",
    ["status"]  # labels: success, idempotent_hit, conflict
)

# outbox_lag_seconds tracks age of oldest unpublished outbox event
outbox_lag_seconds = Gauge(
    "outbox_lag_seconds",
    "Age in seconds of the oldest unpublished outbox event"
)

# dlq_depth tracks how many events are in the dead letter queue
dlq_depth = Gauge(
    "dlq_depth",
    "Number of events currently in the dead letter queue"
)
