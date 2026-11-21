"""Database reset shared by the test suite and the verification harness.

One list of tables, so a table added by a migration is cleaned everywhere.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.tenant import DEFAULT_TENANT_ID

TABLES = (
    "refunds, entries, transfers, payments, wallets, accounts, idempotency_keys, "
    "api_keys, outbox_events, processed_events, webhook_events, recon_items, "
    "recon_runs, settlement_lines"
)
CLEAN_SQL = (
    # Ledger tables are append-only even for the owner; replica mode skips the
    # immutability triggers so a run can start clean (the owner is a
    # superuser in docker-compose and CI).
    "SET session_replication_role = replica",
    f"TRUNCATE TABLE {TABLES} RESTART IDENTITY CASCADE",
    f"DELETE FROM tenants WHERE id <> '{DEFAULT_TENANT_ID}'",
    "SET session_replication_role = DEFAULT",
)


async def reset(owner_url: str) -> None:
    """Empty every table except the platform tenant. Owner role only."""
    engine = create_async_engine(owner_url)
    async with engine.begin() as conn:
        for statement in CLEAN_SQL:
            await conn.execute(text(statement))
    await engine.dispose()
