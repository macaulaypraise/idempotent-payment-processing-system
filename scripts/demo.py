"""make demo: the three-minute demo (project guide, section 9), scripted.

    make dev
    make demo                 # DEMO_PAUSE=2 to pause between steps for a recording

Runs against the dev stack and checks every claim it makes against the
database and the provider's own records, not against the API's answers:

  1. 1,000 concurrent requests with one idempotency key -> one payment
  2. Redis stopped, same again -> still one payment
  3. the provider charges, then times out -> CAPTURED, one charge
  4. the provider's webhooks replayed twice each, and late copies under new
     event ids, the refund's before the charge's -> no state change, no
     ledger entry posted twice
  5. reconciliation: lines matched, fees posted, one injected mismatch flagged

Each step prints PASS or FAIL; the exit code is non-zero if any failed.
Record it with `asciinema rec -c "make demo"`.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx

from scripts.stack import (
    API_URL,
    SIM_URL,
    Stack,
    compose,
    wait_healthy,
    webhook_secret,
)

PAUSE = float(os.environ.get("DEMO_PAUSE", "0"))
CONCURRENT = int(os.environ.get("DEMO_CONCURRENT", "1000"))


class Demo:
    def __init__(self, stack: Stack, api: httpx.AsyncClient, sim: httpx.AsyncClient):
        self.stack = stack
        self.api = api
        self.sim = sim
        self.headers: dict[str, str] = {}
        self.wallet = ""
        self.failed: list[str] = []

    # --- output -----------------------------------------------------------------

    @staticmethod
    def title(number: int, text: str) -> None:
        print(f"\n\033[1m{number}. {text}\033[0m", flush=True)

    @staticmethod
    def say(text: str) -> None:
        print(f"   {text}", flush=True)

    def check(self, ok: bool, claim: str) -> None:
        mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
        print(f"   {mark}  {claim}", flush=True)
        if not ok:
            self.failed.append(claim)

    async def pause(self) -> None:
        if PAUSE:
            await asyncio.sleep(PAUSE)

    # --- helpers -----------------------------------------------------------------

    async def post(self, path: str, body: dict[str, Any], key: str) -> httpx.Response:
        return await self.api.post(
            path, json=body, headers={**self.headers, "Idempotency-Key": key}
        )

    async def payment(self, payment_id: str) -> dict[str, Any]:
        response = await self.api.get(
            f"/v1/payments/{payment_id}", headers=self.headers
        )
        response.raise_for_status()
        return dict(response.json())

    async def wait_for(
        self, payment_id: str, statuses: set[str], seconds: float
    ) -> str:
        deadline = time.monotonic() + seconds
        status = ""
        while time.monotonic() < deadline:
            status = str((await self.payment(payment_id))["status"]).upper()
            if status in statuses:
                return status
            await asyncio.sleep(0.5)
        return status

    async def charges(self, payment_id: str) -> list[dict[str, Any]]:
        response = await self.sim.get(
            "/charges", params={"merchant_reference": payment_id}
        )
        response.raise_for_status()
        return list(response.json())

    async def pay(self, amount: int = 2_500) -> str:
        body = {"amount": amount, "currency": "USD", "wallet_id": self.wallet}
        response = await self.post("/v1/payments", body, str(uuid.uuid4()))
        response.raise_for_status()
        return str(response.json()["payment_id"])

    async def setup(self) -> None:
        api_key = await self.stack.platform_api_key()
        self.headers = {"Authorization": f"Bearer {api_key}"}
        response = await self.post(
            "/v1/wallets", {"currency": "USD"}, str(uuid.uuid4())
        )
        response.raise_for_status()
        self.wallet = response.json()["wallet_id"]
        await self.sim.put("/admin/faults", json={})  # a well-behaved provider
        self.say(f"platform API key created; wallet {self.wallet}")

    # --- 1 and 2: one key, many requests ------------------------------------------

    async def storm(self, label: str) -> None:
        key = str(uuid.uuid4())
        body = {"amount": 1_000, "currency": "USD", "wallet_id": self.wallet}
        started = time.monotonic()
        results = await asyncio.gather(
            *[self.post("/v1/payments", body, key) for _ in range(CONCURRENT)],
            return_exceptions=True,
        )
        took = time.monotonic() - started
        codes = Counter(
            r.status_code if isinstance(r, httpx.Response) else type(r).__name__
            for r in results
        )
        ids = {
            r.json()["payment_id"]
            for r in results
            if isinstance(r, httpx.Response) and r.status_code in (200, 202)
        }
        self.say(
            f"{CONCURRENT} requests in {took:.1f}s: "
            + ", ".join(f"{n}x {c}" for c, n in sorted(codes.items(), key=str))
        )
        if 409 in codes:
            self.say(
                "409 = the same key was still in flight after the lock wait: "
                "the client retries later (not a failure)"
            )
        else:
            self.say("every request waited for the first one, then got its answer")
        rows = await self.stack.scalar(
            "SELECT count(*) FROM payments WHERE idempotency_key = :key", key=key
        )
        self.check(rows == 1, f"{label}: {rows} payment row(s) in Postgres for the key")
        self.check(
            len(ids) == 1, f"{label}: every answered request got the same payment id"
        )
        if ids:
            payment_id = next(iter(ids))
            await self.wait_for(payment_id, {"CAPTURED", "SETTLED"}, 30)
            charged = await self.charges(payment_id)
            self.check(
                len(charged) == 1, f"{label}: {len(charged)} charge at the provider"
            )

    async def one_key(self) -> None:
        self.title(1, f"{CONCURRENT:,} concurrent requests with one idempotency key")
        await self.storm("Redis up")
        await self.pause()

    async def redis_down(self) -> None:
        self.title(2, "Redis stopped: the same again")
        compose("stop", "redis")
        self.say("docker compose stop redis")
        try:
            await self.storm("Redis down")
        finally:
            compose("start", "redis")
            self.say(
                "docker compose start redis (Redis is a cache; Postgres holds the keys)"
            )
        await self.pause()

    # --- 3: charge then timeout ---------------------------------------------------

    async def charge_then_timeout(self) -> str:
        self.title(3, "The provider charges, then never answers")
        await self.sim.put("/admin/faults", json={"charge_then_timeout_rate": 1.0})
        self.say("provider fault profile: charge_then_timeout_rate = 1.0")
        try:
            payment_id = await self.pay()
            self.say(f"payment {payment_id} accepted; the charger's call times out...")
            status = await self.wait_for(payment_id, {"CAPTURED", "SETTLED"}, 60)
        finally:
            await self.sim.put("/admin/faults", json={})
        charged = await self.charges(payment_id)
        self.say(
            "the retry (or the provider's webhook) found the original charge, "
            "because every attempt sends the same provider key"
        )
        self.check(status in ("CAPTURED", "SETTLED"), f"payment ends {status}")
        self.check(len(charged) == 1, f"{len(charged)} charge at the provider")
        await self.pause()
        return payment_id

    # --- 4: webhooks twice, out of order ------------------------------------------

    async def deliver(self, event: dict[str, Any]) -> int:
        """POST a signed provider event to our webhook endpoint."""
        from app.core.security import SIGNATURE_HEADER, sign_webhook

        body = json.dumps(event, separators=(",", ":")).encode()
        signature = sign_webhook(webhook_secret(), body, int(time.time()))
        response = await self.api.post(
            "/v1/webhooks/psp",
            content=body,
            headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
        )
        return response.status_code

    async def ledger_state(
        self, payment_id: str, refund_id: str
    ) -> tuple[str, int, int]:
        status = (await self.payment(payment_id))["status"].upper()
        transfers = await self.stack.scalar(
            "SELECT count(*) FROM transfers WHERE reference_id::text IN (:p, :r)",
            p=payment_id,
            r=refund_id or payment_id,
        )
        events = await self.stack.scalar(
            "SELECT count(*) FROM webhook_events "
            "WHERE payload::text LIKE :p OR payload::text LIKE :r",
            p=f"%{payment_id}%",
            r=f"%{refund_id or payment_id}%",
        )
        return status, int(transfers or 0), int(events or 0)

    async def webhooks(self, payment_id: str) -> None:
        self.title(4, "The provider's webhooks, twice each, refund before charge")
        response = await self.post(
            "/v1/refunds", {"payment_id": payment_id}, str(uuid.uuid4())
        )
        refund_id = response.json().get("refund_id", "")
        self.say(f"refund {refund_id} requested ({response.status_code})")
        status = await self.wait_for(payment_id, {"REFUNDED"}, 60)
        before = await self.ledger_state(payment_id, refund_id)
        self.say(
            f"before: payment {before[0]}, {before[1]} ledger transfers, "
            f"{before[2]} webhook events stored"
        )
        sent = [
            w
            for w in (await self.sim.get("/admin/webhooks")).json()
            if payment_id in w["body"] or (refund_id and refund_id in w["body"])
        ]
        refund_first = sorted(sent, key=lambda w: 0 if "refund" in w["type"] else 1)
        # (a) the provider's own events again: same event ids
        for webhook in refund_first:
            for _ in range(2):
                await self.sim.post(f"/admin/webhooks/{webhook['id']}/redeliver")
                await self.sim.post("/admin/webhooks/drain")
            self.say(f"provider redelivered {webhook['type']} twice (same event id)")
        # (b) late copies under new event ids, signed like the provider's, so
        # the inbox keeps them and the processor has to decide: refund first
        for webhook in refund_first:
            event = json.loads(webhook["body"])
            event["id"] = f"evt_late_{uuid.uuid4().hex[:16]}"
            for _ in range(2):
                code = await self.deliver(event)
            self.say(f"late copy of {webhook['type']} sent twice ({code})")
        await asyncio.sleep(3)  # the webhook processor polls
        after = await self.ledger_state(payment_id, refund_id)
        self.say(
            f"after:  payment {after[0]}, {after[1]} ledger transfers, "
            f"{after[2]} webhook events stored"
        )
        self.check(status == "REFUNDED" and after[0] == before[0], "no state change")
        self.check(after[1] == before[1], "no ledger entry posted twice")
        self.check(
            after[2] == before[2] + len(refund_first),
            "each event stored once: the redeliveries were dropped by the inbox, "
            "each late copy kept once and found already applied",
        )
        await self.pause()

    # --- 5: reconciliation --------------------------------------------------------

    async def reconcile(self) -> None:
        self.title(5, "Reconciliation against the provider's settlement file")
        today = datetime.now(UTC).date().isoformat()
        payments = [await self.pay(amount) for amount in (1_200, 3_400, 5_600)]
        for payment_id in payments:
            await self.wait_for(payment_id, {"CAPTURED"}, 30)
        target = (await self.charges(payments[0]))[0]["id"]
        await self.sim.post(
            f"/admin/settlements/{today}/faults",
            json={"faults": [{"kind": "amount_mismatch", "psp_reference": target}]},
        )
        self.say(f"injected: the file reports a wrong amount for charge {target}")
        try:
            response = await self.api.post(
                "/v1/reconciliations", json={"date": today}, headers=self.headers
            )
            response.raise_for_status()
            run = response.json()
        finally:
            await self.sim.delete(f"/admin/settlements/{today}/faults")
        self.say(
            f"run for {today} (the whole day in this database): {run['lines']} "
            f"lines, {json.dumps(run['counts'])}, {run['posted']} postings"
        )
        if run["unexplained"] > 1:
            self.say(
                f"{run['unexplained'] - 1} unexplained item(s) predate this demo "
                "(older captures in this database); the checks below are about "
                "this demo's own payments. On a fresh stack the run shows only "
                "the injected one."
            )
        detail = (
            await self.api.get(
                f"/v1/reconciliations/{run['id']}",
                params={"classification": "amount_mismatch"},
                headers=self.headers,
            )
        ).json()
        flagged = {str(i["payment_id"]) for i in detail.get("items") or []}
        statuses = {p: (await self.payment(p))["status"].upper() for p in payments}
        fees = await self.stack.scalar(
            "SELECT count(*) FROM entries e JOIN accounts a ON a.id = e.account_id "
            "JOIN transfers t ON t.id = e.transfer_id "
            "WHERE a.kind = 'psp_fees' AND t.reference_id::text = ANY(:ids)",
            ids=payments[1:],
        )
        self.say(f"this demo's payments: {statuses}")
        self.check(
            payments[0] in flagged and statuses[payments[0]] == "CAPTURED",
            "the injected mismatch is flagged, and its payment is left unsettled",
        )
        self.check(
            all(statuses[p] == "SETTLED" for p in payments[1:]),
            "the matching lines settled their payments",
        )
        self.check(int(fees or 0) == 2, f"a provider fee posted for each ({fees})")
        await self.pause()

    async def run(self) -> int:
        print(f"\033[1mLedgerline demo\033[0m against {API_URL} (provider {SIM_URL})")
        await self.setup()
        await self.one_key()
        await self.redis_down()
        payment_id = await self.charge_then_timeout()
        await self.webhooks(payment_id)
        await self.reconcile()
        print()
        if self.failed:
            print(f"\033[31m{len(self.failed)} check(s) failed\033[0m")
            return 1
        print("\033[32mevery check passed\033[0m")
        return 0


async def main_async() -> int:
    await wait_healthy()
    stack = Stack()
    limits = httpx.Limits(max_connections=CONCURRENT, max_keepalive_connections=100)
    try:
        async with (
            httpx.AsyncClient(base_url=API_URL, timeout=60.0, limits=limits) as api,
            httpx.AsyncClient(base_url=SIM_URL, timeout=30.0) as sim,
        ):
            return await Demo(stack, api, sim).run()
    finally:
        await stack.close()


def main() -> None:  # pragma: no cover - CLI wiring
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":  # pragma: no cover
    main()
