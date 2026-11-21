"""
Migrations against data, in a scratch database: v1-shaped rows survive the
upgrade (tenant backfill, Numeric -> BIGINT minor units) and the downgrade.
"""

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import DEFAULT_TENANT_ID, TEST_APP_ROLE

pytestmark = pytest.mark.asyncio


@pytest.fixture
def scratch_url(owner_database_url: str) -> Iterator[str]:
    base = make_url(owner_database_url)
    name = f"{base.database}_migrations"
    admin = base.set(database="postgres").render_as_string(hide_password=False)

    async def _admin(sql: str) -> None:
        engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(sql))
        await engine.dispose()

    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    yield base.set(database=name).render_as_string(hide_password=False)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = url
    cfg.attributes["app_role"] = TEST_APP_ROLE
    return cfg


async def _query(url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        rows = [tuple(r) for r in (await conn.execute(text(sql))).all()]
    await engine.dispose()
    return rows


async def _execute(url: str, sql: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text(sql))
    await engine.dispose()


async def test_v1_rows_survive_upgrade_and_downgrade(scratch_url: str) -> None:
    payment_id = uuid.uuid4()
    await asyncio.to_thread(command.upgrade, _alembic(scratch_url), "f7355da969c5")
    await _execute(
        scratch_url,
        "INSERT INTO payments (id, idempotency_key, amount, currency, version, status, "
        f"created_at) VALUES ('{payment_id}', 'v1-key', 10.50, 'USD', 1, 'SETTLED', now())",
    )

    await asyncio.to_thread(command.upgrade, _alembic(scratch_url), "head")
    [(amount, tenant)] = await _query(
        scratch_url, f"SELECT amount, tenant_id FROM payments WHERE id = '{payment_id}'"
    )
    assert amount == 1050
    assert tenant == DEFAULT_TENANT_ID
    columns = {
        r[0]
        for r in await _query(
            scratch_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'payments'",
        )
    }
    assert "version" not in columns

    await asyncio.to_thread(command.downgrade, _alembic(scratch_url), "f7355da969c5")
    [(amount_after,)] = await _query(
        scratch_url, f"SELECT amount FROM payments WHERE id = '{payment_id}'"
    )
    assert str(amount_after) == "10.50"


async def test_outbox_backlog_is_numbered_oldest_first(scratch_url: str) -> None:
    """Stage 5 adds outbox_events.seq: rows already waiting are numbered by
    created_at (not physical order), and new rows continue after them."""
    await asyncio.to_thread(command.upgrade, _alembic(scratch_url), "e6c1a9d4b582")
    for name, age in (("middle", 2), ("newest", 1), ("oldest", 3)):
        await _execute(
            scratch_url,
            "INSERT INTO outbox_events (id, tenant_id, event_type, payload, created_at) "
            f"VALUES ('{uuid.uuid4()}', '{DEFAULT_TENANT_ID}', '{name}', '{{}}', "
            f"now() - interval '{age} minutes')",
        )

    await asyncio.to_thread(command.upgrade, _alembic(scratch_url), "head")
    await _execute(
        scratch_url,
        "INSERT INTO outbox_events (id, tenant_id, event_type, payload, created_at) "
        f"VALUES ('{uuid.uuid4()}', '{DEFAULT_TENANT_ID}', 'after', '{{}}', now())",
    )
    rows = await _query(
        scratch_url, "SELECT event_type, seq FROM outbox_events ORDER BY seq"
    )
    assert rows == [("oldest", 1), ("middle", 2), ("newest", 3), ("after", 4)]

    await asyncio.to_thread(command.downgrade, _alembic(scratch_url), "e6c1a9d4b582")
    tables = {
        r[0]
        for r in await _query(
            scratch_url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'",
        )
    }
    assert "webhook_events" not in tables


async def test_stage6_reconciliation_tables_come_and_go(scratch_url: str) -> None:
    await asyncio.to_thread(command.upgrade, _alembic(scratch_url), "head")
    tables = {
        r[0]
        for r in await _query(
            scratch_url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'",
        )
    }
    assert {"settlement_lines", "recon_runs", "recon_items"} <= tables

    await asyncio.to_thread(command.downgrade, _alembic(scratch_url), "a7f3c2d8e190")
    columns = {
        r[0]
        for r in await _query(
            scratch_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'payments'",
        )
    }
    assert "settlement_line_id" not in columns
