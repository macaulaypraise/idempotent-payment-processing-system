from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )

    # App
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # Database. The app connects as a restricted role (APP_DB_ROLE) that
    # cannot update or delete ledger rows; migrations connect as the owner.
    DATABASE_URL: str
    MIGRATION_DATABASE_URL: str | None = None  # owner; falls back to DATABASE_URL
    APP_DB_ROLE: str = "ipps_app"
    APP_DB_PASSWORD: str | None = None  # set by `python -m app.cli configure-app-role`
    DB_ECHO: bool = False  # log every SQL statement; never enable in production

    # Redis
    REDIS_URL: str

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str
    KAFKA_SECURITY_PROTOCOL: str = "PLAINTEXT"
    KAFKA_SASL_MECHANISM: str = "SCRAM-SHA-256"
    KAFKA_SASL_USERNAME: str = ""
    KAFKA_SASL_PASSWORD: str = ""

    # Payment provider (the PSP simulator locally). Replaces v1's
    # PAYMENT_PROVIDER_URL, which is no longer read.
    PSP_BASE_URL: str = "http://localhost:8001"
    PSP_TIMEOUT_SECONDS: float = 3.0  # a timeout means "unknown", never "failed"
    # Charger leases: a claimed payment is retried once this passes, so it must
    # comfortably exceed PSP_TIMEOUT_SECONDS.
    PSP_LEASE_SECONDS: float = 30.0
    PSP_MAX_ATTEMPTS: int = 6  # then a dlq.v1 event; the payment stays PROCESSING
    PSP_RETRY_BASE_SECONDS: float = 2.0  # exponential backoff with jitter
    PSP_RETRY_MAX_SECONDS: float = 300.0
    SWEEPER_INTERVAL_SECONDS: float = 5.0
    SWEEPER_BATCH_SIZE: int = 50
    # PENDING this long means the payment.initiated event was lost
    SWEEPER_PENDING_AFTER_SECONDS: float = 60.0
    # Provider webhooks: HMAC secret shared with the provider, and how far a
    # signature timestamp may be from our clock before the request is refused.
    PSP_WEBHOOK_SECRET: str = ""  # empty rejects every webhook (fail closed)
    PSP_WEBHOOK_TOLERANCE_SECONDS: float = 300.0
    WEBHOOK_MAX_BODY_BYTES: int = 65536
    # Webhook processor: events that cannot apply yet are parked and retried
    WEBHOOK_POLL_SECONDS: float = 0.5
    WEBHOOK_BATCH_SIZE: int = 50
    WEBHOOK_MAX_ATTEMPTS: int = 10  # then dead + dlq.v1
    WEBHOOK_RETRY_BASE_SECONDS: float = 1.0
    WEBHOOK_RETRY_MAX_SECONDS: float = 300.0
    # Outbox relay (ADR 0005): one leader holds an advisory lock and wakes on
    # NOTIFY; the fallback poll covers a missed notification, and standbys try
    # for the lock this often, so it also bounds failover time.
    OUTBOX_BATCH_SIZE: int = 100
    OUTBOX_FALLBACK_POLL_SECONDS: float = 5.0
    # Reconciliation: the provider name stored on settlement lines, and how
    # many days after a capture its settlement line may still arrive. The run
    # for date D reports captures from D - window that no file mentioned.
    RECON_PROVIDER: str = "psp"
    RECON_SETTLEMENT_WINDOW_DAYS: int = 1
    # Deliberate bugs for the verification harness's self-test
    # (app/core/planted_bugs.py). Comma-separated; ignored in production.
    HARNESS_PLANTED_BUGS: str = ""
    # Crash points for testing, e.g. "charger.after_provider_call=exit".
    # Ignored when APP_ENV=production.
    FAILPOINTS: str = ""

    # Idempotency (Postgres is the source of truth; Redis only caches)
    IDEMPOTENCY_TTL_SECONDS: int = 86400  # key rows expire after 24 hours
    # How long a duplicate waits for the first request's transaction before 409
    IDEMPOTENCY_LOCK_TIMEOUT_MS: int = 2000
    IDEMPOTENCY_KEY_MAX_LENGTH: int = 255
    # Redis cache: short timeouts so a dead or blackholed Redis cannot stall
    # requests, and a breaker that skips Redis for a while after a failure
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 0.25
    REDIS_BREAKER_COOLDOWN_SECONDS: float = 5.0
    IDEMPOTENCY_EXPIRY_INTERVAL_SECONDS: int = 60
    IDEMPOTENCY_EXPIRY_BATCH_SIZE: int = 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
