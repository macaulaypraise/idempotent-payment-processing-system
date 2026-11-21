"""
Load test for IPPS v2.

    make tenant NAME=loadtest            # prints api_key=...
    export IPPS_API_KEY=<that key>
    make load-test                       # or tests/load/redis_kill.sh

Every client-side duplicate is reported as a Locust failure whose message
starts with "DUPLICATE": a retry, or a concurrent copy of the same request,
that comes back with a different payment_id.
"""

import os
import random
import uuid
from typing import Any

import gevent
from gevent.pool import Group
from locust import HttpUser, between, task

API_KEY = os.environ.get("IPPS_API_KEY", "")
PAYMENTS = "/v1/payments"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}", "Idempotency-Key": key}


def _body() -> dict[str, Any]:
    return {"amount": round(random.uniform(1.00, 500.00), 2), "currency": "USD"}


class LegitimateUser(HttpUser):
    """Creates payments (3), retries the last one with the same key and body
    (1), and reads payment status (1)."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        if not API_KEY:
            raise RuntimeError("set IPPS_API_KEY (make tenant NAME=loadtest)")
        self.last_key: str | None = None
        self.last_body: dict[str, Any] | None = None
        self.last_payment_id: str | None = None

    @task(3)
    def create_payment(self) -> None:
        key, body = str(uuid.uuid4()), _body()
        with self.client.post(
            PAYMENTS,
            json=body,
            headers=_headers(key),
            catch_response=True,
            name=PAYMENTS,
        ) as resp:
            if resp.status_code == 202:
                self.last_key, self.last_body = key, body
                self.last_payment_id = resp.json().get("payment_id")
                resp.success()
            else:
                resp.failure(f"create failed: {resp.status_code} {resp.text[:200]}")

    @task(1)
    def retry_same_key(self) -> None:
        if not (self.last_key and self.last_body and self.last_payment_id):
            return
        with self.client.post(
            PAYMENTS,
            json=self.last_body,
            headers=_headers(self.last_key),
            catch_response=True,
            name=f"{PAYMENTS} (retry)",
        ) as resp:
            if resp.status_code != 202:
                resp.failure(f"retry failed: {resp.status_code} {resp.text[:200]}")
                return
            returned = resp.json().get("payment_id")
            if returned != self.last_payment_id:
                resp.failure(
                    f"DUPLICATE: original={self.last_payment_id} retry={returned}"
                )
            else:
                resp.success()

    @task(1)
    def get_payment(self) -> None:
        if not self.last_payment_id:
            return
        with self.client.get(
            f"{PAYMENTS}/{self.last_payment_id}",
            headers={"Authorization": f"Bearer {API_KEY}"},
            catch_response=True,
            name=f"{PAYMENTS}/{{id}}",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"get failed: {resp.status_code} {resp.text[:200]}")


class ConcurrentDuplicateUser(HttpUser):
    """Fires the same request N times at once with one key. All copies must
    return 202 with the same payment_id (a 409 means the first was still in
    flight past lock_timeout, which is allowed but counted)."""

    wait_time = between(0.5, 1.5)
    copies = 5

    def on_start(self) -> None:
        if not API_KEY:
            raise RuntimeError("set IPPS_API_KEY (make tenant NAME=loadtest)")

    @task
    def concurrent_same_key(self) -> None:
        key, body = str(uuid.uuid4()), _body()
        ids: list[str] = []

        def send() -> None:
            with self.client.post(
                PAYMENTS,
                json=body,
                headers=_headers(key),
                catch_response=True,
                name=f"{PAYMENTS} (concurrent x{self.copies})",
            ) as resp:
                if resp.status_code == 202:
                    ids.append(resp.json()["payment_id"])
                    resp.success()
                elif resp.status_code == 409:
                    resp.success()  # still in flight; a client would retry
                else:
                    resp.failure(f"{resp.status_code} {resp.text[:200]}")

        group = Group()
        for _ in range(self.copies):
            group.spawn(send)
        group.join()
        gevent.sleep(0)

        if len(set(ids)) > 1:
            self.environment.events.request.fire(
                request_type="CHECK",
                name="duplicate-check",
                response_time=0,
                response_length=0,
                exception=Exception(
                    f"DUPLICATE: one key -> payments {sorted(set(ids))}"
                ),
                context={},
            )
