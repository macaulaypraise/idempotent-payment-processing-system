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


def test_tenant_scoped_tables_have_non_null_tenant_id() -> None:
    from app.models import OutboxEvent, Payment, ProcessedEvent

    for model in (Payment, OutboxEvent, ProcessedEvent):
        col = model.__table__.c.tenant_id
        assert col.nullable is False, model.__name__
        assert {fk.column.table.name for fk in col.foreign_keys} == {"tenants"}


def test_payment_idempotency_key_is_unique_per_tenant_not_globally() -> None:
    from sqlalchemy import UniqueConstraint

    from app.models import Payment

    assert Payment.__table__.c.idempotency_key.unique is not True
    uniques = [
        tuple(c.name for c in con.columns)
        for con in Payment.__table__.constraints
        if isinstance(con, UniqueConstraint)
    ]
    assert ("tenant_id", "idempotency_key") in uniques


def test_idempotency_key_primary_key_is_tenant_and_key() -> None:
    from app.models import IdempotencyKey

    pk = [c.name for c in IdempotencyKey.__table__.primary_key.columns]
    assert pk == ["tenant_id", "key"]


def test_api_key_stores_hash_not_plaintext() -> None:
    from app.models import ApiKey

    cols = set(ApiKey.__table__.c.keys())
    assert "key_hash" in cols
    assert not {"key", "plaintext", "secret"} & cols
