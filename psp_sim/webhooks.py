"""Signed webhook delivery, with the faults real providers have.

Every charge and refund the simulator records produces one webhook event,
stored before the HTTP response is sent (so a charge whose response is lost
still produces its webhook). Delivery is at least once:

  - a non-2xx response or a network error is retried with backoff,
  - webhook_duplicate_rate sends the same signed event twice,
  - webhook_drop_rate never sends it (until redelivered via the admin API),
  - webhook_delay_max_seconds delays it, which reorders events.

The sender is injectable: production posts over HTTP; in-process tests pass
a function that calls the API app directly and drive delivery with drain().
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.security import SIGNATURE_HEADER, sign_webhook
from psp_sim.faults import Faults
from psp_sim.store import Store, Webhook

# (url, body, headers) -> HTTP status; 0 for a network error
Sender = Callable[[str, bytes, dict[str, str]], Awaitable[int]]


def http_sender(timeout_seconds: float = 5.0) -> Sender:
    client = httpx.AsyncClient(timeout=timeout_seconds)

    async def send(url: str, body: bytes, headers: dict[str, str]) -> int:
        try:
            response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError:
            return 0
        return response.status_code

    return send


class WebhookDispatcher:
    def __init__(
        self,
        store: Store,
        faults: Faults,
        *,
        url: str | None,
        secret: str,
        sender: Sender | None = None,
        max_attempts: int = 8,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.faults = faults
        self.url = url
        self.secret = secret
        self.sender = sender or http_sender()
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.clock = clock
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def enqueue(self, event_type: str, data: dict[str, Any]) -> Webhook | None:
        """Record a webhook for later delivery. Fault draws happen only when
        webhooks are enabled, so seeded runs without them are unchanged."""
        if not self.enabled:
            return None
        profile = self.faults.profile
        dropped = self.faults.roll(profile.webhook_drop_rate)
        duplicate = self.faults.roll(profile.webhook_duplicate_rate)
        delay = (
            self.faults.uniform(0, profile.webhook_delay_max_seconds)
            if profile.webhook_delay_max_seconds > 0
            else 0.0
        )
        now = self.clock()
        event_id = f"evt_{uuid.uuid4().hex[:24]}"
        body = json.dumps(
            {"id": event_id, "type": event_type, "created": int(now), "data": data},
            separators=(",", ":"),
        )
        webhook = Webhook(
            id=event_id,
            type=event_type,
            body=body,
            status="dropped" if dropped else "pending",
            duplicate=int(duplicate),
            attempts=0,
            next_attempt_at=now + delay,
            last_status=None,
            created_at=datetime.now(UTC).isoformat(),
            delivered_at=None,
        )
        self.store.insert_webhook(webhook)
        self._wake.set()
        return webhook

    def redeliver(self, webhook_id: str) -> bool:
        if self.store.webhook(webhook_id) is None:
            return False
        self.store.update_webhook(
            webhook_id, status="pending", next_attempt_at=self.clock()
        )
        self._wake.set()
        return True

    async def deliver(self, webhook: Webhook) -> bool:
        """One delivery attempt (two sends for a duplicate); True if accepted."""
        assert self.url is not None
        body = webhook.body.encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign_webhook(self.secret, body, int(self.clock())),
        }
        status = await self.sender(self.url, body, headers)
        if 200 <= status < 300 and webhook.duplicate:
            await self.sender(self.url, body, headers)
        attempts = webhook.attempts + 1
        if 200 <= status < 300:
            self.store.update_webhook(
                webhook.id,
                status="delivered",
                attempts=attempts,
                last_status=status,
                delivered_at=datetime.now(UTC).isoformat(),
            )
            return True
        if attempts >= self.max_attempts:
            self.store.update_webhook(
                webhook.id, status="failed", attempts=attempts, last_status=status
            )
            return False
        delay = min(self.retry_max_seconds, self.retry_base_seconds * 2**attempts)
        self.store.update_webhook(
            webhook.id,
            attempts=attempts,
            last_status=status,
            next_attempt_at=self.clock() + delay,
        )
        return False

    async def drain(self, *, due_only: bool = False) -> int:
        """Attempt every pending webhook once (only due ones if due_only),
        oldest due first; returns how many were accepted."""
        if not self.enabled:
            return 0
        pending = self.store.due_webhooks(self.clock() if due_only else None)
        accepted = 0
        for webhook in pending:
            accepted += await self.deliver(webhook)
        return accepted

    async def run(self, idle_seconds: float = 0.5) -> None:  # pragma: no cover
        """Background loop for the containerised simulator."""
        while True:
            try:
                await self.drain(due_only=True)
            except Exception:  # noqa: BLE001, S110 - keep delivering
                pass
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=idle_seconds)
            except TimeoutError:
                pass
