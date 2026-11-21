"""The simulator's webhooks: signed, stored before the response, and
delivered at least once with drop, duplicate and delay faults."""

import json
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.security import SIGNATURE_HEADER, verify_webhook
from psp_sim.main import create_app

SECRET = "whsec_sim"
URL = "http://api/v1/webhooks/psp"
CHARGE = {"amount": 1_000, "currency": "USD", "merchant_reference": "pay_1"}


class Recorder:
    """A sender that records what it was given and answers with `status`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []
        self.status = 200

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.sent.append((url, body, headers))
        return self.status

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(body) for _, body, _ in self.sent]


class Sim:
    def __init__(self, app: Any, client: AsyncClient, recorder: Recorder) -> None:
        self.app = app
        self.client = client
        self.recorder = recorder
        self.dispatcher = app.state.webhooks

    async def faults(self, **profile: Any) -> None:
        resp = await self.client.put("/admin/faults", json=profile)
        assert resp.status_code == 200, resp.text

    async def charge(self, key: str = "k1", **overrides: Any) -> dict[str, Any]:
        resp = await self.client.post(
            "/charges", json={**CHARGE, **overrides}, headers={"Idempotency-Key": key}
        )
        assert resp.status_code in (200, 201), resp.text
        return dict(resp.json())


@pytest_asyncio.fixture
async def sim() -> AsyncIterator[Sim]:
    recorder = Recorder()
    app = create_app(
        db_path=":memory:",
        response_timeout_seconds=0.2,
        webhook_url=URL,
        webhook_secret=SECRET,
        webhook_sender=recorder,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sim"
    ) as c:
        yield Sim(app, c, recorder)


async def test_a_charge_produces_one_signed_webhook(sim: Sim) -> None:
    charge = await sim.charge()
    await sim.charge()  # a replay creates nothing new
    assert await sim.dispatcher.drain() == 1

    [(url, body, headers)] = sim.recorder.sent
    assert url == URL
    verify_webhook(
        SECRET,
        body,
        headers[SIGNATURE_HEADER],
        now=json.loads(body)["created"],
        tolerance_seconds=300,
    )
    event = json.loads(body)
    assert event["type"] == "charge.succeeded"
    assert event["id"].startswith("evt_")
    assert event["data"]["id"] == charge["id"]
    assert event["data"]["merchant_reference"] == "pay_1"
    assert sim.dispatcher.store.webhook(event["id"]).status == "delivered"


async def test_declines_and_refunds_produce_their_own_event_types(sim: Sim) -> None:
    await sim.faults(decline_rate=1.0)
    await sim.charge("k-declined")
    await sim.faults()
    charge = await sim.charge("k-ok", merchant_reference="pay_2")
    resp = await sim.client.post(
        "/refunds",
        json={
            "charge_id": charge["id"],
            "amount": 1_000,
            "merchant_reference": "ref_1",
        },
        headers={"Idempotency-Key": "r1"},
    )
    assert resp.status_code == 201
    assert resp.json()["merchant_reference"] == "ref_1"
    await sim.dispatcher.drain()
    types = [e["type"] for e in sim.recorder.events()]
    assert types == ["charge.failed", "charge.succeeded", "refund.succeeded"]
    assert sim.recorder.events()[-1]["data"]["merchant_reference"] == "ref_1"


async def test_charge_then_timeout_still_produces_the_webhook(sim: Sim) -> None:
    """The response is lost; the webhook is how the merchant can still learn."""
    await sim.faults(charge_then_timeout_rate=1.0)
    await sim.client.post(
        "/charges", json=CHARGE, headers={"Idempotency-Key": "lost"}
    )  # ASGITransport waits out the hang; the store already has the charge
    assert await sim.dispatcher.drain() == 1
    assert sim.recorder.events()[0]["type"] == "charge.succeeded"


async def test_non_2xx_is_retried_then_given_up(sim: Sim) -> None:
    sim.recorder.status = 500
    await sim.charge()
    [webhook] = sim.dispatcher.store.webhooks()
    for _ in range(sim.dispatcher.max_attempts):
        assert await sim.dispatcher.drain() == 0
    assert sim.dispatcher.store.webhook(webhook.id).status == "failed"
    assert len(sim.recorder.sent) == sim.dispatcher.max_attempts

    sim.recorder.status = 200  # redelivery from the admin API
    resp = await sim.client.post(f"/admin/webhooks/{webhook.id}/redeliver")
    assert resp.status_code == 200
    assert (await sim.client.post("/admin/webhooks/drain")).json() == {"accepted": 1}


async def test_retry_waits_for_its_backoff(sim: Sim) -> None:
    sim.recorder.status = 0  # network error
    await sim.charge()
    await sim.dispatcher.drain(due_only=True)
    assert await sim.dispatcher.drain(due_only=True) == 0
    assert len(sim.recorder.sent) == 1  # the retry is not due yet


async def test_dropped_webhooks_are_never_sent_until_redelivered(sim: Sim) -> None:
    await sim.faults(webhook_drop_rate=1.0)
    await sim.charge()
    assert await sim.dispatcher.drain() == 0
    assert sim.recorder.sent == []
    [dropped] = (await sim.client.get("/admin/webhooks?status=dropped")).json()
    await sim.client.post(f"/admin/webhooks/{dropped['id']}/redeliver")
    assert await sim.dispatcher.drain() == 1


async def test_duplicates_send_the_identical_signed_request_twice(sim: Sim) -> None:
    await sim.faults(webhook_duplicate_rate=1.0)
    await sim.charge()
    await sim.dispatcher.drain()
    first, second = sim.recorder.sent
    assert first == second


async def test_delayed_webhooks_are_not_due_yet(sim: Sim) -> None:
    await sim.faults(webhook_delay_max_seconds=3600.0, seed=1)
    await sim.charge()
    assert await sim.dispatcher.drain(due_only=True) == 0
    assert await sim.dispatcher.drain() == 1  # drain() without due_only sends all


async def test_unknown_webhook_redeliver_is_404(sim: Sim) -> None:
    assert (await sim.client.post("/admin/webhooks/nope/redeliver")).status_code == 404


async def test_reset_clears_webhooks(sim: Sim) -> None:
    await sim.charge()
    await sim.client.post("/admin/reset")
    assert (await sim.client.get("/admin/webhooks")).json() == []


async def test_without_a_url_nothing_is_queued() -> None:
    app = create_app(db_path=":memory:", webhook_url=None)
    app.state.webhooks.url = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://s") as c:
        await c.post("/charges", json=CHARGE, headers={"Idempotency-Key": "k"})
    assert app.state.store.webhooks() == []
    assert await app.state.webhooks.drain() == 0


async def test_refund_retry_from_an_older_client_still_replays(sim: Sim) -> None:
    """merchant_reference is metadata, not part of the refund fingerprint."""
    charge = await sim.charge()
    body = {"charge_id": charge["id"], "amount": 1_000}
    first = await sim.client.post(
        "/refunds", json=body, headers={"Idempotency-Key": "r"}
    )
    again = await sim.client.post(
        "/refunds",
        json={**body, "merchant_reference": "ref"},
        headers={"Idempotency-Key": "r"},
    )
    assert first.status_code == 201 and again.status_code == 200


def test_store_from_an_earlier_version_gains_the_new_columns(tmp_path: Any) -> None:
    from psp_sim.store import Store

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE refunds (
            id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL, charge_id TEXT NOT NULL,
            amount INTEGER NOT NULL, status TEXT NOT NULL,
            failure_reason TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO refunds VALUES ('re_1','k','f','ch_1',1,'succeeded',NULL,'t');
        """
    )
    old.close()

    store = Store(path)
    refund = store.refund_by_key("k")
    assert refund is not None and refund.merchant_reference is None
    new, created = store.insert_refund(
        key="k2",
        fingerprint="f",
        charge_id="ch_1",
        amount=1,
        status="failed",
        failure_reason="x",
        merchant_reference="ref",
    )
    assert created and store.refund_by_key("k2") == new
