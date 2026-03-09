import random
import uuid
from typing import Any

from locust import HttpUser, between, task


class LegitimateUser(HttpUser):
    """
    Simulates a legitimate API client that:
    1. Creates payments (weight 3)
    2. Retries with same idempotency key and body (weight 1) <- proves no double charge
    3. Polls payment status (weight 1)
    """

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        self.last_key: str | None = None
        self.last_body: dict[str, Any] | None = None
        self.last_payment_id: str | None = None

    @task(3)
    def create_payment(self) -> None:
        idempotency_key = str(uuid.uuid4())
        body = {
            "amount": round(random.uniform(1.00, 500.00), 2),
            "currency": "USD",
        }

        with self.client.post(
            "/payments",
            json=body,
            headers={"Idempotency-Key": idempotency_key},
            catch_response=True,
            name="/payments",
        ) as resp:
            if resp.status_code == 202:
                data = resp.json()
                self.last_key = idempotency_key
                self.last_body = body
                self.last_payment_id = data.get("payment_id")
                resp.success()
            else:
                resp.failure(f"Create failed: {resp.status_code} -- {resp.text[:200]}")

    @task(1)
    def retry_same_key(self) -> None:
        if not self.last_key or not self.last_body or not self.last_payment_id:
            return

        with self.client.post(
            "/payments",
            json=self.last_body,
            headers={"Idempotency-Key": self.last_key},
            catch_response=True,
            name="/payments",
        ) as resp:
            if resp.status_code in (200, 202):
                data = resp.json()
                returned_id = data.get("payment_id")
                if returned_id != self.last_payment_id:
                    resp.failure(
                        f"DUPLICATE CHARGE DETECTED: "
                        f"original={self.last_payment_id}, retry={returned_id}"
                    )
                else:
                    resp.success()
            else:
                resp.failure(f"Retry failed: {resp.status_code} -- {resp.text[:200]}")

    @task(1)
    def get_payment(self) -> None:
        if not self.last_payment_id:
            return

        with self.client.get(
            f"/payments/{self.last_payment_id}",
            catch_response=True,
            name="/payments/{id}",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(
                    f"Get payment failed: {resp.status_code} -- {resp.text[:200]}"
                )


class MaliciousUser(HttpUser):
    """Spams the exact same idempotency key concurrently to try and break the lock"""

    wait_time = between(1, 2)

    @task
    def concurrent_spam(self):
        key = str(uuid.uuid4())
        body = {"amount": 10.00, "currency": "USD"}

        # Fire 3 requests at the exact same time
        for _ in range(3):
            self.client.post(
                "/payments",
                json=body,
                headers={"Idempotency-Key": key},
                name="/payments (Concurrent Spam)",
            )
