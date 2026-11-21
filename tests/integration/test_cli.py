"""app.cli against real PostgreSQL."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import create_api_key, create_tenant, revoke_api_key
from app.core.security import hash_api_key
from app.models.tenant import DEFAULT_TENANT_ID, ApiKey

pytestmark = pytest.mark.asyncio


async def test_create_tenant_stores_only_the_hash(db_session: AsyncSession) -> None:
    tenant_id, plaintext = await create_tenant(db_session, "acme")
    row = (
        await db_session.execute(select(ApiKey).where(ApiKey.tenant_id == tenant_id))
    ).scalar_one()
    assert row.key_hash == hash_api_key(plaintext)
    assert row.prefix == plaintext[:12]


async def test_create_api_key_for_default_tenant_and_revoke(
    db_session: AsyncSession,
) -> None:
    plaintext = await create_api_key(db_session, DEFAULT_TENANT_ID)
    assert await revoke_api_key(db_session, plaintext[:12]) == 1
    assert await revoke_api_key(db_session, plaintext[:12]) == 0


async def test_create_api_key_for_unknown_tenant_fails(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(ValueError):
        await create_api_key(db_session, uuid.uuid4())
