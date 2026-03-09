"""
Unit tests for app/config.py
Verifies that Settings reads environment variables correctly
and that the lru_cache singleton pattern works.
"""

from unittest.mock import patch

# Settings reads from environment


def test_settings_reads_app_env() -> None:
    with patch.dict("os.environ", {"APP_ENV": "test"}):
        from importlib import reload

        import app.config as cfg

        reload(cfg)
        settings = cfg.Settings()
        assert settings.APP_ENV == "test"


def test_settings_reads_redis_url() -> None:
    url = "redis://localhost:6379/1"
    with patch.dict("os.environ", {"REDIS_URL": url}):
        from app.config import Settings

        s = Settings()
        assert s.REDIS_URL == url


def test_settings_reads_database_url() -> None:
    url = "postgresql+asyncpg://user:pass@localhost/testdb"
    with patch.dict("os.environ", {"DATABASE_URL": url}):
        from app.config import Settings

        s = Settings()
        assert s.DATABASE_URL == url


def test_settings_reads_kafka_bootstrap_servers() -> None:
    with patch.dict("os.environ", {"KAFKA_BOOTSTRAP_SERVERS": "kafka:9092"}):
        from app.config import Settings

        s = Settings()
        assert "kafka:9092" in s.KAFKA_BOOTSTRAP_SERVERS


def test_settings_has_default_for_idempotency_ttl() -> None:
    from app.config import Settings

    s = Settings()
    assert s.IDEMPOTENCY_TTL_SECONDS > 0


def test_settings_has_default_for_lock_ttl() -> None:
    from app.config import Settings

    s = Settings()
    assert s.IDEMPOTENCY_LOCK_TTL_SECONDS > 0


def test_settings_has_default_for_max_retries() -> None:
    from app.config import Settings

    s = Settings()
    assert s.PAYMENT_MAX_RETRIES > 0


# get_settings() returns the same instance (lru_cache singleton)


def test_get_settings_is_singleton() -> None:
    from app.config import get_settings

    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


# All required fields present


def test_required_settings_fields_exist() -> None:
    from app.config import Settings

    fields = Settings.model_fields.keys()
    required = [
        "APP_ENV",
        "DATABASE_URL",
        "REDIS_URL",
        "KAFKA_BOOTSTRAP_SERVERS",
        "IDEMPOTENCY_TTL_SECONDS",
        "PAYMENT_MAX_RETRIES",
    ]
    for field in required:
        assert field in fields, f"Missing required setting: {field}"
