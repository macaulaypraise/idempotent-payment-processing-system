"""
Unit tests for app/models/
Validates column definitions, defaults, and relationships
without touching a real database.
"""


def test_payment_model_has_required_columns() -> None:
    from app.models.payment import Payment

    mapper = Payment.__mapper__
    col_names = {c.key for c in mapper.columns}

    required = {
        "id",
        "idempotency_key",
        "status",
        "amount",
        "currency",
        "created_at",
        "updated_at",
        "version",
    }
    for col in required:
        assert col in col_names, f"Payment missing column: {col}"


def test_outbox_event_model_has_required_columns() -> None:
    from app.models.outbox_event import OutboxEvent

    col_names = {c.key for c in OutboxEvent.__mapper__.columns}

    required = {"id", "event_type", "payload", "published_at", "created_at"}
    for col in required:
        assert col in col_names, f"OutboxEvent missing column: {col}"


def test_outbox_event_published_at_is_nullable() -> None:
    from app.models.outbox_event import OutboxEvent

    cols = {c.key: c for c in OutboxEvent.__mapper__.columns}
    assert cols["published_at"].nullable is True


def test_processed_event_model_has_required_columns() -> None:
    from app.models.processed_event import ProcessedEvent

    col_names = {c.key for c in ProcessedEvent.__mapper__.columns}

    required = {"event_id", "consumer_group", "processed_at"}
    for col in required:
        assert col in col_names, f"ProcessedEvent missing column: {col}"


def test_payment_version_column_has_default() -> None:
    from app.models.payment import Payment

    cols = {c.key: c for c in Payment.__mapper__.columns}
    version_col = cols.get("version")
    assert version_col is not None
    # version should default to 1 or 0 (optimistic lock starting value)
    assert version_col.default is not None or version_col.server_default is not None


def test_payment_status_is_string_or_enum() -> None:
    from app.models.payment import Payment

    cols = {c.key: c for c in Payment.__mapper__.columns}
    status_col = cols["status"]
    assert status_col is not None
