"""
/v1/wallets and /v1/transfers against real PostgreSQL, through the HTTP layer.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ledger import AccountKind
from app.models.wallet import Wallet
from app.services.ledger import Reference, credit, debit, post_transfer, system_accounts
from verification.invariants import check_ledger

pytestmark = pytest.mark.asyncio
Factory = async_sessionmaker[AsyncSession]


def _h(creds: Any, key: str | None = None) -> dict[str, str]:
    headers = dict(creds.headers)
    headers["Idempotency-Key"] = key or str(uuid.uuid4())
    return headers


async def _create_wallet(client: Any, creds: Any, currency: str = "USD") -> str:
    resp = await client.post(
        "/v1/wallets", json={"currency": currency}, headers=_h(creds)
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["wallet_id"])


async def _fund(
    factory: Factory, tenant_id: uuid.UUID, wallet_id: str, amount: int
) -> None:
    """Test-only money in. Stage 3 credits wallets from captured payments."""
    async with factory() as db:
        wallet = await db.get(Wallet, uuid.UUID(wallet_id))
        assert wallet is not None
        system = await system_accounts(
            db, tenant_id=tenant_id, currency=wallet.currency
        )
        await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference("test_funding", "test", uuid.uuid4()),
            lines=[
                debit(system[AccountKind.PSP_CLEARING], amount),
                credit(wallet.available_account_id, amount),
            ],
        )
        await db.commit()


async def _balance(client: Any, creds: Any, wallet_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/wallets/{wallet_id}/balance", headers=creds.headers)
    assert resp.status_code == 200
    return dict(resp.json())


async def test_create_wallet_is_idempotent_and_starts_empty(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        first = await client.post(
            "/v1/wallets", json={"currency": "NGN"}, headers=_h(creds, "w1")
        )
        again = await client.post(
            "/v1/wallets", json={"currency": "NGN"}, headers=_h(creds, "w1")
        )
        balance = await _balance(client, creds, first.json()["wallet_id"])

    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert balance == {
        "wallet_id": first.json()["wallet_id"],
        "currency": "NGN",
        "available": 0,
        "pending": 0,
    }


async def test_overdraft_is_422_and_retry_returns_the_same_422(
    api_client: Any, make_tenant: Any
) -> None:
    """Exit gate: a rejection is stored under the key, not recomputed."""
    creds = await make_tenant()
    async with api_client() as client:
        a, b = await _create_wallet(client, creds), await _create_wallet(client, creds)
        body = {"from_wallet_id": a, "to_wallet_id": b, "amount": 500}
        first = await client.post("/v1/transfers", json=body, headers=_h(creds, "t1"))
        retry = await client.post("/v1/transfers", json=body, headers=_h(creds, "t1"))

    assert first.status_code == retry.status_code == 422
    assert first.json() == retry.json()
    assert first.json()["detail"] == "Insufficient funds"
    assert retry.headers["Idempotent-Replayed"] == "true"


async def test_stored_rejection_stands_even_after_funds_arrive(
    api_client: Any, make_tenant: Any, session_factory: Factory
) -> None:
    """A retry is the same request, so it gets the same answer. A new attempt
    needs a new key."""
    creds = await make_tenant()
    async with api_client() as client:
        a, b = await _create_wallet(client, creds), await _create_wallet(client, creds)
        body = {"from_wallet_id": a, "to_wallet_id": b, "amount": 500}
        await client.post("/v1/transfers", json=body, headers=_h(creds, "t1"))
        await _fund(session_factory, creds.tenant_id, a, 1_000)
        retry = await client.post("/v1/transfers", json=body, headers=_h(creds, "t1"))
        fresh = await client.post("/v1/transfers", json=body, headers=_h(creds, "t2"))
        assert (await _balance(client, creds, a))["available"] == 500

    assert retry.status_code == 422
    assert fresh.status_code == 201


async def test_transfer_moves_money_and_is_idempotent(
    api_client: Any, make_tenant: Any, session_factory: Factory
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        a, b = await _create_wallet(client, creds), await _create_wallet(client, creds)
        await _fund(session_factory, creds.tenant_id, a, 1_000)
        body = {"from_wallet_id": a, "to_wallet_id": b, "amount": 250}
        first = await client.post("/v1/transfers", json=body, headers=_h(creds, "t"))
        retry = await client.post("/v1/transfers", json=body, headers=_h(creds, "t"))
        bal_a, bal_b = (
            await _balance(client, creds, a),
            await _balance(client, creds, b),
        )
        entries_b = await client.get(f"/v1/wallets/{b}/entries", headers=creds.headers)

    assert first.status_code == 201
    assert first.json() == retry.json()
    assert first.json()["amount"] == 250 and first.json()["currency"] == "USD"
    assert (bal_a["available"], bal_b["available"]) == (750, 250)
    [entry] = entries_b.json()["entries"]
    assert entry["transfer_id"] == first.json()["transfer_id"]
    assert (entry["account"], entry["direction"], entry["amount"]) == (
        "available",
        "credit",
        250,
    )
    async with session_factory() as db:
        assert await check_ledger(db, creds.tenant_id) == []


async def test_entries_are_paginated_newest_first(
    api_client: Any, make_tenant: Any, session_factory: Factory
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        a, b = await _create_wallet(client, creds), await _create_wallet(client, creds)
        await _fund(session_factory, creds.tenant_id, a, 100)
        for amount in (1, 2, 3, 4, 5):
            body = {"from_wallet_id": a, "to_wallet_id": b, "amount": amount}
            await client.post("/v1/transfers", json=body, headers=_h(creds))
        page1 = (
            await client.get(f"/v1/wallets/{b}/entries?limit=2", headers=creds.headers)
        ).json()
        page2 = (
            await client.get(
                f"/v1/wallets/{b}/entries?limit=2&before={page1['next_before']}",
                headers=creds.headers,
            )
        ).json()
        page3 = (
            await client.get(
                f"/v1/wallets/{b}/entries?limit=2&before={page2['next_before']}",
                headers=creds.headers,
            )
        ).json()

    assert [e["amount"] for e in page1["entries"]] == [5, 4]
    assert [e["amount"] for e in page2["entries"]] == [3, 2]
    assert [e["amount"] for e in page3["entries"]] == [1]
    assert page3["next_before"] is None


async def test_wallets_are_tenant_scoped(api_client: Any, make_tenant: Any) -> None:
    alice, bob = await make_tenant("alice"), await make_tenant("bob")
    async with api_client() as client:
        alices = await _create_wallet(client, alice)
        bobs = await _create_wallet(client, bob)
        peek = await client.get(f"/v1/wallets/{alices}/balance", headers=bob.headers)
        entries = await client.get(f"/v1/wallets/{alices}/entries", headers=bob.headers)
        steal = await client.post(
            "/v1/transfers",
            json={"from_wallet_id": alices, "to_wallet_id": bobs, "amount": 1},
            headers=_h(bob),
        )
    assert peek.status_code == entries.status_code == 404
    assert steal.status_code == 404


async def test_cross_currency_transfer_is_rejected(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        usd, ngn = (
            await _create_wallet(client, creds, "USD"),
            await _create_wallet(client, creds, "NGN"),
        )
        resp = await client.post(
            "/v1/transfers",
            json={"from_wallet_id": usd, "to_wallet_id": ngn, "amount": 1},
            headers=_h(creds),
        )
    assert resp.status_code == 422
    assert "FX is not supported" in resp.json()["detail"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a, b: {"from_wallet_id": a, "to_wallet_id": a, "amount": 1},
        lambda a, b: {"from_wallet_id": a, "to_wallet_id": b, "amount": 1.5},
        lambda a, b: {"from_wallet_id": a, "to_wallet_id": b, "amount": 0},
        lambda a, b: {"from_wallet_id": a, "to_wallet_id": "nope", "amount": 1},
    ],
)
async def test_invalid_transfer_bodies_are_422(
    api_client: Any, make_tenant: Any, mutate: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        a, b = await _create_wallet(client, creds), await _create_wallet(client, creds)
        resp = await client.post("/v1/transfers", json=mutate(a, b), headers=_h(creds))
    assert resp.status_code == 422


async def test_unknown_wallet_on_balance_is_404(
    api_client: Any, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        resp = await client.get(
            f"/v1/wallets/{uuid.uuid4()}/balance", headers=creds.headers
        )
    assert resp.status_code == 404
