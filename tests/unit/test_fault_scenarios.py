"""The fault layer's plans (verification/scenario.py) and the runner's pure
parts. The runs themselves need the fault stack: make verify-faults."""

import json
import random
from pathlib import Path

import pytest

from verification import scenario
from verification.faults import Client, Request, _seeds, container
from verification.history import History

SEEDS = range(60)


def test_same_seed_same_plan_other_seed_other_plan() -> None:
    assert scenario.build(7) == scenario.build(7)
    assert scenario.build(7) != scenario.build(8)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_fault_is_undone_inside_the_run(seed: int) -> None:
    plan = scenario.build(seed, seconds=40)
    opened: dict[tuple[str, str], float] = {}
    for event in plan.events:
        assert 0 < event.at < plan.seconds, event
        if event.action == "toxic_on":
            opened[("toxic", event.params["name"])] = event.at
        elif event.action == "toxic_off":
            assert opened.pop(("toxic", event.params["name"])) < event.at
        elif event.action == "failpoint_on":
            opened[("failpoint", event.target)] = event.at
        elif event.action == "failpoint_off":
            assert opened.pop(("failpoint", event.target)) < event.at
    assert not opened, f"faults left on: {opened}"
    kills = [e.target for e in plan.events if e.action == "kill"]
    starts = [e.target for e in plan.events if e.action == "start"]
    assert sorted(kills) == sorted(starts)
    assert [e.at for e in plan.events] == sorted(e.at for e in plan.events)


def test_the_plans_cover_every_fault_type_target_and_crash_point() -> None:
    plans = [scenario.build(seed) for seed in SEEDS]
    events = [e for plan in plans for e in plan.events]
    toxics = {(e.params["kind"], e.target) for e in events if e.action == "toxic_on"}
    assert {kind for kind, _ in toxics} == set(scenario.NETWORK_FAULTS)
    assert {proxy for _, proxy in toxics} == set(scenario.PROXIES)
    assert {e.target for e in events if e.action == "kill"} == set(scenario.KILLABLE)
    armed = {e.params["failpoint"] for e in events if e.action == "failpoint_on"}
    assert armed == set(scenario.FAILPOINTS)
    assert {p.sim_preset for p in plans} == set(scenario.SIM_PRESETS)


def test_a_crash_point_is_armed_in_every_service_that_reaches_it() -> None:
    for seed in SEEDS:
        for event in scenario.build(seed).events:
            if event.action == "failpoint_on":
                services = scenario.FAILPOINTS[event.params["failpoint"]]
                assert event.target == ",".join(services)
            if event.action == "failpoint_off":
                armed_at = next(
                    e.at
                    for e in scenario.build(seed).events
                    if e.action == "failpoint_on"
                )
                assert event.at - armed_at >= 8.0  # outlasts the restart
    # the sweeper charges through the charger's code
    assert "sweeper" in scenario.FAILPOINTS["charger.after_provider_call"]


def test_toxics_are_valid_toxiproxy_payloads() -> None:
    rng = random.Random(0)
    for kind in scenario.NETWORK_FAULTS:
        for _ in range(20):
            spec = scenario.toxic(kind, rng)
            assert spec["type"] in {"latency", "reset_peer", "timeout"}
            assert spec["stream"] in {"upstream", "downstream"}
            assert all(isinstance(v, int) for v in spec["attributes"].values())
    with pytest.raises(ValueError, match="unknown"):
        scenario.toxic("flood", rng)


def test_a_plan_is_json_and_describes_itself() -> None:
    plan = scenario.build(3)
    assert json.loads(json.dumps(plan.as_dict()))["seed"] == 3
    assert plan.describe().startswith(f"provider={plan.sim_preset}; ")
    with pytest.raises(ValueError, match="15 seconds"):
        scenario.build(1, seconds=10)


def test_the_self_test_loses_every_provider_response() -> None:
    plan = scenario.selftest()
    on, off = plan.events
    assert (on.action, on.target, off.action) == ("toxic_on", "psp", "toxic_off")
    # and every webhook, or they heal the payment before any retry happens
    assert scenario.sim_profile(plan.sim_preset) == {"webhook_drop_rate": 1.0}
    # beyond the charger's 2 s timeout in docker-compose.faults.yml
    assert on.params["attributes"]["latency"] > 2_000
    compose = Path("docker-compose.faults.yml").read_text()
    assert 'PSP_TIMEOUT_SECONDS: "2"' in compose


def test_every_preset_is_a_simulator_profile() -> None:
    from verification.machine import FAULT_PRESETS

    for preset in scenario.SIM_PRESETS:
        assert scenario.sim_profile(preset) == FAULT_PRESETS[preset]


def test_the_proxies_in_the_plans_are_the_proxies_in_the_config() -> None:
    proxies = json.loads(Path("verification/toxiproxy.json").read_text())
    assert {p["name"] for p in proxies} == set(scenario.PROXIES)


def test_seeds_and_container_names() -> None:
    class Args:
        seeds = "5, 9"
        seed = None
        scenarios = 3

    assert _seeds(Args()) == [5, 9]  # type: ignore[arg-type]
    Args.seeds, Args.seed = "", 100
    assert _seeds(Args()) == [100, 101, 102]  # type: ignore[arg-type]
    assert container("charger-1") == "ipps-faults-charger-1"


class _Transport:
    """Answers from a script: an exception or (status, body) per request."""

    def __init__(self, script: list[object]) -> None:
        self.script = script

    def __call__(self, request: object) -> object:
        import httpx

        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        status, body = step  # type: ignore[misc]
        return httpx.Response(status, json=body)


async def test_the_client_retries_unknowns_with_the_same_key_until_answered() -> None:
    import httpx

    answer = {"payment_id": "p1"}
    transport = httpx.MockTransport(
        _Transport(
            [
                httpx.ConnectError("reset"),
                (503, {"detail": "down"}),
                (409, {"detail": "in flight"}),
                (202, answer),
            ]
        )
    )
    history = History()
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        client = Client(http, {}, history, __import__("collections").Counter())
        request = Request("create_payment", "/v1/payments", "k1", {"amount": 1})
        got = await client.until_answered(request, deadline=float("inf"))
    assert got == answer
    assert [o.outcome for o in history.client()] == [
        "unknown",
        "unknown",
        "unknown",
        "ok",
    ]
    assert {o.key for o in history.client()} == {"k1"}
    assert {json.dumps(o.request) for o in history.client()} == {'{"amount": 1}'}
    assert client.stats["client retries"] == 3


async def test_an_unanswered_request_is_kept_for_after_healing() -> None:
    import httpx

    transport = httpx.MockTransport(_Transport([httpx.ReadTimeout("slow")]))
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        client = Client(http, {}, History(), __import__("collections").Counter())
        request = Request("create_refund", "/v1/refunds", "k2", {"payment_id": "p"})
        assert await client.until_answered(request, deadline=0.0) is None
    assert client.unanswered == [request]
