from functools import lru_cache
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    #App
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

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

@lru_cache()
def get_settings() -> Settings:
    return Settings()


