"""PSP client error mapping, failpoints, and charger event decoding."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from app.clients.psp import PspClient, PspRejected, PspUnavailable
from app.core import failpoints
from app.core.failpoints import SimulatedCrash, arm, armed, disarm, failpoint
from app.workers.charger import decode


def _client(handler: Any, timeout: float = 1.0) -> PspClient:
    transport = httpx.MockTransport(handler)
    return PspClient(
        httpx.AsyncClient(transport=transport, base_url="http://psp"), timeout
    )


async def _charge(client: PspClient) -> Any:
    return await client.charge(
        key="k", amount=100, currency="USD", merchant_reference="p"
    )


@pytest.mark.parametrize("status", [500, 502, 503, 408, 429])
async def test_server_errors_mean_outcome_unknown(status: int) -> None:
    with pytest.raises(PspUnavailable):
        await _charge(_client(lambda r: httpx.Response(status)))


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_client_errors_are_rejections(status: int) -> None:
    with pytest.raises(PspRejected):
        await _charge(_client(lambda r: httpx.Response(status, text="no")))


async def test_transport_error_means_outcome_unknown() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PspUnavailable):
        await _charge(_client(boom))


async def test_timeout_means_outcome_unknown() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(201, json={})

    with pytest.raises(PspUnavailable):
        await _charge(_client(slow, timeout=0.05))


async def test_charge_sends_the_idempotency_key_and_maps_the_result() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["Idempotency-Key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": "ch_1",
                "status": "declined",
                "decline_reason": "card_declined",
            },
        )

    result = await _charge(_client(handler))
    assert seen == {
        "key": "k",
        "body": {"amount": 100, "currency": "USD", "merchant_reference": "p"},
    }
    assert (result.charge_id, result.succeeded, result.decline_reason) == (
        "ch_1",
        False,
        "card_declined",
    )


async def test_refund_maps_the_result() -> None:
    client = _client(
        lambda r: httpx.Response(201, json={"id": "re_1", "status": "succeeded"})
    )
    result = await client.refund(key="r", charge_id="ch_1", amount=100)
    assert (result.refund_id, result.succeeded) == ("re_1", True)


# --- failpoints -----------------------------------------------------------------


def test_unarmed_failpoint_does_nothing() -> None:
    failpoint("nothing.armed.here")


def test_raise_mode_raises_a_base_exception() -> None:
    with armed("x"), pytest.raises(SimulatedCrash):
        try:
            failpoint("x")
        except Exception:  # noqa: BLE001 - must not catch a simulated crash
            pytest.fail("SimulatedCrash was caught by `except Exception`")


def test_exit_mode_kills_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(failpoints.os, "_exit", exits.append)
    arm("y", "exit")
    try:
        failpoint("y")
    finally:
        disarm("y")
    assert exits == [failpoints.EXIT_CODE]


def test_failpoints_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    settings = get_settings().model_copy(update={"FAILPOINTS": "a=raise, b"})
    monkeypatch.setattr(failpoints, "get_settings", lambda: settings)
    with pytest.raises(SimulatedCrash):
        failpoint("a")
    exits: list[int] = []
    monkeypatch.setattr(failpoints.os, "_exit", exits.append)
    failpoint("b")  # no mode means exit
    assert exits == [failpoints.EXIT_CODE]


def test_failpoints_never_fire_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    settings = get_settings().model_copy(update={"APP_ENV": "production"})
    monkeypatch.setattr(failpoints, "get_settings", lambda: settings)
    with armed("z"):
        failpoint("z")


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        arm("x", "explode")


# --- charger event decoding ---------------------------------------------------------


def test_decode_reads_event_id_and_type_from_headers() -> None:
    event_id = uuid.uuid4()
    event = decode(
        "payment.initiated",
        '{"payment_id": "p", "tenant_id": "t"}',
        [("event_id", str(event_id).encode()), ("event_type", b"payment.initiated")],
    )
    assert event.event_id == event_id
    assert event.event_type == "payment.initiated"
    assert event.payload == {"payment_id": "p", "tenant_id": "t"}


def test_decode_without_headers_has_no_event_id() -> None:
    event = decode("refund.created", '{"refund_id": "r"}', None)
    assert event.event_id is None and event.event_type == "refund.created"
