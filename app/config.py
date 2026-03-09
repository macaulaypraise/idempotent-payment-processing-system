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

    # Redis
    REDIS_URL: str

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str

    # Mock Payment Provider
    PAYMENT_PROVIDER_URL: str = "http://localhost:8001"

    # Test configuration fields
    IDEMPOTENCY_TTL_SECONDS: int = 86400  # Default 24 hours
    IDEMPOTENCY_LOCK_TTL_SECONDS: int = 30  # Default 30 seconds
    PAYMENT_MAX_RETRIES: int = 3  # Default 3 retries


@lru_cache
def get_settings() -> Settings:
    return Settings()
