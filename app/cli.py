"""Tenant and API key administration.

    python -m app.cli create-tenant --name acme
    python -m app.cli create-api-key --tenant-id <uuid|default>
    python -m app.cli revoke-api-key --prefix ipps_AbCdEfG

The plaintext key is printed once and never stored.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionFactory
from app.core.security import generate_api_key
from app.models.tenant import DEFAULT_TENANT_ID, ApiKey, Tenant


async def create_tenant(db: AsyncSession, name: str) -> tuple[uuid.UUID, str]:
    tenant = Tenant(id=uuid.uuid4(), name=name)
    db.add(tenant)
    await db.flush()
    plaintext = await _add_key(db, tenant.id)
    await db.commit()
    return tenant.id, plaintext


async def create_api_key(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    exists = await db.execute(select(Tenant.id).where(Tenant.id == tenant_id))
    if exists.scalar_one_or_none() is None:
        raise ValueError(f"tenant {tenant_id} does not exist")
    plaintext = await _add_key(db, tenant_id)
    await db.commit()
    return plaintext


async def revoke_api_key(db: AsyncSession, prefix: str) -> int:
    result = await db.execute(
        update(ApiKey)
        .where(ApiKey.prefix == prefix, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _add_key(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    plaintext, prefix, key_hash = generate_api_key()
    db.add(
        ApiKey(id=uuid.uuid4(), tenant_id=tenant_id, key_hash=key_hash, prefix=prefix)
    )
    await db.flush()
    return plaintext


def _tenant_id(value: str) -> uuid.UUID:
    return DEFAULT_TENANT_ID if value == "default" else uuid.UUID(value)


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionFactory() as db:
        if args.command == "create-tenant":
            tenant_id, key = await create_tenant(db, args.name)
            print(f"tenant_id={tenant_id}")
            print(f"api_key={key}")
            print("Store the key now; it cannot be shown again.", file=sys.stderr)
        elif args.command == "create-api-key":
            key = await create_api_key(db, _tenant_id(args.tenant_id))
            print(f"api_key={key}")
            print("Store the key now; it cannot be shown again.", file=sys.stderr)
        elif args.command == "revoke-api-key":
            count = await revoke_api_key(db, args.prefix)
            print(f"revoked={count}")
            return 0 if count else 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    tenant = sub.add_parser("create-tenant", help="create a tenant and its first key")
    tenant.add_argument("--name", required=True)
    key = sub.add_parser("create-api-key", help="add a key to an existing tenant")
    key.add_argument("--tenant-id", required=True, help="tenant UUID or 'default'")
    revoke = sub.add_parser("revoke-api-key", help="revoke a key by its prefix")
    revoke.add_argument("--prefix", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
