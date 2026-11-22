"""The PSP simulator: idempotency, replays, faults, refunds, ground truth."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from psp_sim.faults import FaultProfile, Faults
from psp_sim.main import create_app

CHARGE = {"amount": 1_000, "currency": "USD", "merchant_reference": "pay_1"}


@pytest_asyncio.fixture
async def sim() -> AsyncIterator[AsyncClient]:
    app = create_app(db_path=":memory:", response_timeout_seconds=0.2)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sim"
    ) as c:
        yield c


async def _faults(sim: AsyncClient, **profile: Any) -> None:
    resp = await sim.put("/admin/faults", json=profile)
    assert resp.status_code == 200, resp.text


async def _charge(
    sim: AsyncClient, key: str = "k1", body: dict[str, Any] = CHARGE
) -> Any:
    return await sim.post("/charges", json=body, headers={"Idempotency-Key": key})


async def test_charge_and_replay(sim: AsyncClient) -> None:
    first = await _charge(sim)
    again = await _charge(sim)
    assert first.status_code == 201 and again.status_code == 200
    assert first.json() == again.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.json()["status"] == "succeeded"
    truth = (await sim.get("/ground-truth")).json()
    assert len(truth["charges"]) == 1


async def test_key_reuse_with_a_different_body_is_422(sim: AsyncClient) -> None:
    await _charge(sim)
    resp = await _charge(sim, body={**CHARGE, "amount": 999})
    assert resp.status_code == 422


async def test_missing_key_is_400(sim: AsyncClient) -> None:
    assert (await sim.post("/charges", json=CHARGE)).status_code == 400


async def test_concurrent_first_requests_charge_once(sim: AsyncClient) -> None:
    await _faults(sim, slow_rate=1.0, slow_seconds=0.05)
    results = await asyncio.gather(*[_charge(sim) for _ in range(10)])
    assert len({r.json()["id"] for r in results}) == 1
    assert len((await sim.get("/ground-truth")).json()["charges"]) == 1


async def test_charge_then_timeout_records_then_hangs_but_replay_is_instant(
    sim: AsyncClient,
) -> None:
    await _faults(sim, charge_then_timeout_rate=1.0)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _charge(sim)
    truth = (await sim.get("/ground-truth")).json()
    assert len(truth["charges"]) == 1  # the effect happened
    async with asyncio.timeout(0.05):  # the replay does not hang
        replay = await _charge(sim)
    assert replay.json()["id"] == truth["charges"][0]["id"]


async def test_unavailable_records_nothing(sim: AsyncClient) -> None:
    await _faults(sim, unavailable_rate=1.0)
    assert (await _charge(sim)).status_code == 503
    assert (await sim.get("/ground-truth")).json()["charges"] == []
    await _faults(sim)
    assert (await _charge(sim)).status_code == 201


async def test_decline(sim: AsyncClient) -> None:
    await _faults(sim, decline_rate=1.0)
    body = (await _charge(sim)).json()
    assert body["status"] == "declined" and body["decline_reason"] == "card_declined"


async def test_lookup_by_id_and_reference(sim: AsyncClient) -> None:
    charge = (await _charge(sim)).json()
    assert (await sim.get(f"/charges/{charge['id']}")).json() == charge
    assert (
        await sim.get("/charges", params={"merchant_reference": "pay_1"})
    ).json() == [charge]
    assert (await sim.get("/charges/ch_nope")).status_code == 404


async def test_refund_rules(sim: AsyncClient) -> None:
    charge = (await _charge(sim)).json()

    async def refund(
        key: str, charge_id: str = charge["id"], amount: int = 1_000
    ) -> Any:
        return await sim.post(
            "/refunds",
            json={"charge_id": charge_id, "amount": amount},
            headers={"Idempotency-Key": key},
        )

    assert (await refund("r0", amount=1)).status_code == 422  # full refunds only
    assert (await refund("r0", charge_id="ch_nope")).status_code == 404
    ok = await refund("r1")
    assert ok.status_code == 201 and ok.json()["status"] == "succeeded"
    assert (await refund("r1")).json() == ok.json()  # replay
    second = (await refund("r2")).json()
    assert (second["status"], second["failure_reason"]) == (
        "failed",
        "already_refunded",
    )


async def test_refund_of_a_declined_charge_fails(sim: AsyncClient) -> None:
    await _faults(sim, decline_rate=1.0)
    charge = (await _charge(sim)).json()
    resp = await sim.post(
        "/refunds",
        json={"charge_id": charge["id"], "amount": 1_000},
        headers={"Idempotency-Key": "r"},
    )
    assert resp.json()["failure_reason"] == "charge_not_succeeded"


async def test_invalid_fault_profile_is_422(sim: AsyncClient) -> None:
    assert (
        await sim.put("/admin/faults", json={"decline_rate": 1.5})
    ).status_code == 422


async def test_reset_clears_store_and_faults(sim: AsyncClient) -> None:
    await _faults(sim, decline_rate=1.0)
    await _charge(sim)
    await sim.post("/admin/reset")
    assert (await sim.get("/ground-truth")).json() == {"charges": [], "refunds": []}
    assert (await sim.get("/admin/faults")).json()["decline_rate"] == 0.0


def test_same_seed_same_decisions() -> None:
    profile = FaultProfile(decline_rate=0.5)
    a, b = Faults(profile, seed=42), Faults(profile, seed=42)
    assert [a.roll(0.5) for _ in range(50)] == [b.roll(0.5) for _ in range(50)]
    c = Faults(profile, seed=43)
    assert [a.roll(0.5) for _ in range(50)] != [c.roll(0.5) for _ in range(50)]


def test_store_survives_restart(tmp_path: Any) -> None:
    from psp_sim.store import Store

    path = str(tmp_path / "sim.db")
    first = Store(path)
    charge, _ = first.insert_charge(
        key="k",
        fingerprint="f",
        merchant_reference="r",
        amount=1,
        currency="USD",
        status="succeeded",
        decline_reason=None,
    )
    again = Store(path)
    assert again.charge_by_key("k") == charge


async def test_an_injected_delay_loses_the_response_after_the_charge_is_recorded() -> (
    None
):
    """The harness's deterministic "lost response": the delay raises, the
    client sees a timeout, and the provider still has the charge."""
    from httpx import ASGITransport, AsyncClient

    from app.clients.psp import PspClient, PspUnavailable
    from psp_sim.main import create_app

    async def lost(seconds: float) -> None:
        raise TimeoutError("lost")

    app = create_app(db_path=":memory:", delay=lost)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://s"
    ) as http:
        await http.put("/admin/faults", json={"charge_then_timeout_rate": 1.0})
        client = PspClient(http, timeout_seconds=30)
        with pytest.raises(PspUnavailable):
            await client.charge(
                key="k", amount=100, currency="USD", merchant_reference="p"
            )
        await http.put("/admin/faults", json={})
        replay = await client.charge(
            key="k", amount=100, currency="USD", merchant_reference="p"
        )
    assert replay.succeeded
    assert len(app.state.store.ground_truth()["charges"]) == 1
