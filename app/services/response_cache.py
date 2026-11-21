"""Read-through cache of completed idempotent responses (ADR 0001).

Redis is never the source of truth. Every call is wrapped so a Redis failure
costs at most one short timeout, and after a failure a breaker skips Redis
entirely for a cooldown period so a dead Redis is not retried per request.
"""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.metrics import cache_errors_total

logger = structlog.get_logger()


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]
    fingerprint: str


class ResponseCache:
    def __init__(
        self,
        redis: Redis | None,
        ttl_seconds: int,
        cooldown_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._cooldown = cooldown_seconds
        self._monotonic = monotonic
        self._open_until = 0.0

    @staticmethod
    def cache_key(tenant_id: uuid.UUID, key: str) -> str:
        return f"idem:v2:{tenant_id}:{key}"

    @property
    def breaker_open(self) -> bool:
        return self._monotonic() < self._open_until

    def _trip(self, operation: str, exc: Exception) -> None:
        cache_errors_total.labels(operation=operation).inc()
        self._open_until = self._monotonic() + self._cooldown
        logger.warning("idempotency_cache_unavailable", op=operation, error=str(exc))

    async def get(self, tenant_id: uuid.UUID, key: str) -> StoredResponse | None:
        if self._redis is None or self.breaker_open:
            return None
        try:
            raw = await self._redis.get(self.cache_key(tenant_id, key))
        except Exception as exc:  # noqa: BLE001 - any Redis failure means "miss"
            self._trip("get", exc)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return StoredResponse(
                status_code=int(data["status_code"]),
                body=dict(data["body"]),
                fingerprint=str(data["fingerprint"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            # A malformed entry is a miss; Postgres still has the truth.
            logger.warning("idempotency_cache_corrupt_entry", error=str(exc))
            return None

    async def put(
        self, tenant_id: uuid.UUID, key: str, response: StoredResponse
    ) -> None:
        """Call only after the transaction that stored the response committed."""
        if self._redis is None or self.breaker_open:
            return
        payload = json.dumps(
            {
                "status_code": response.status_code,
                "body": response.body,
                "fingerprint": response.fingerprint,
            }
        )
        try:
            await self._redis.set(self.cache_key(tenant_id, key), payload, ex=self._ttl)
        except Exception as exc:  # noqa: BLE001
            self._trip("set", exc)
