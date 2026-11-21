"""HTTP client for the payment provider (the PSP simulator locally).

Every call carries an idempotency key and an explicit timeout. A timeout,
connection error or 5xx raises PspUnavailable: the outcome is *unknown*, and
the only safe response is to ask again later with the same key. A 4xx other
than 408/429 raises PspRejected: retrying the same request will not help.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx


class PspUnavailable(Exception):  # noqa: N818
    """Outcome unknown: timeout, network error, 5xx, 408 or 429."""


class PspRejected(Exception):  # noqa: N818
    """The provider refused the request itself (4xx)."""


@dataclass(frozen=True)
class ChargeResult:
    charge_id: str
    succeeded: bool
    decline_reason: str | None


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    succeeded: bool
    failure_reason: str | None


class PspClient:
    def __init__(self, http: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._http = http
        self._timeout = timeout_seconds

    async def _post(self, path: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            # asyncio.timeout bounds the whole call regardless of transport,
            # so in-process tests get real timeout behaviour too.
            async with asyncio.timeout(self._timeout):
                resp = await self._http.post(
                    path, json=body, headers={"Idempotency-Key": key}
                )
        except (TimeoutError, httpx.TransportError) as exc:
            raise PspUnavailable(f"{path}: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise PspUnavailable(f"{path}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PspRejected(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")
        data: dict[str, Any] = resp.json()
        return data

    async def charge(
        self, *, key: str, amount: int, currency: str, merchant_reference: str
    ) -> ChargeResult:
        data = await self._post(
            "/charges",
            key,
            {
                "amount": amount,
                "currency": currency,
                "merchant_reference": merchant_reference,
            },
        )
        return ChargeResult(
            charge_id=str(data["id"]),
            succeeded=data["status"] == "succeeded",
            decline_reason=data.get("decline_reason"),
        )

    async def refund(
        self,
        *,
        key: str,
        charge_id: str,
        amount: int,
        merchant_reference: str | None = None,
    ) -> RefundResult:
        body: dict[str, Any] = {"charge_id": charge_id, "amount": amount}
        if merchant_reference is not None:
            # Echoed in the refund's webhooks, so they can be matched to us
            body["merchant_reference"] = merchant_reference
        data = await self._post("/refunds", key, body)
        return RefundResult(
            refund_id=str(data["id"]),
            succeeded=data["status"] == "succeeded",
            failure_reason=data.get("failure_reason"),
        )

    async def settlement_file(self, file_date: date) -> str:
        """The provider's settlement CSV for a UTC date."""
        try:
            async with asyncio.timeout(self._timeout * 10):  # files are larger
                resp = await self._http.get(f"/settlements/{file_date.isoformat()}")
        except (TimeoutError, httpx.TransportError) as exc:
            raise PspUnavailable(
                f"settlement {file_date}: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise PspUnavailable(f"settlement {file_date}: HTTP {resp.status_code}")
        return resp.text


def build_psp_client(base_url: str, timeout_seconds: float) -> PspClient:
    http = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds + 1)
    return PspClient(http, timeout_seconds)
