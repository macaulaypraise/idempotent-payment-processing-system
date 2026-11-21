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

    # Database
    DATABASE_URL: str
    DB_ECHO: bool = False  # log every SQL statement; never enable in production

    # Redis
    REDIS_URL: str

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str
    KAFKA_SECURITY_PROTOCOL: str = "PLAINTEXT"
    KAFKA_SASL_MECHANISM: str = "SCRAM-SHA-256"
    KAFKA_SASL_USERNAME: str = ""
    KAFKA_SASL_PASSWORD: str = ""

    # Mock Payment Provider
    PAYMENT_PROVIDER_URL: str = "http://localhost:8001"

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

    PAYMENT_MAX_RETRIES: int = 3  # Default 3 retries


@lru_cache
def get_settings() -> Settings:
    return Settings()
