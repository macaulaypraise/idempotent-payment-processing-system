"""Fault scenarios: a seed expands into a timeline (coding companion 7.4).

A scenario is the plan for one run of the fault stack: which provider fault
profile is active, and at which second each network fault starts and stops,
each process is killed and restarted, and each crash point is armed and
disarmed. The same seed always gives the same plan.

The plan is reproducible; the run is not. Real processes, real sockets and
real time decide the interleaving, so a seed replays the same faults at the
same moments, not the same history. That is the point of this layer: it
covers what the in-process state machine deliberately leaves out.

Four fault types (staged build guide, Stage 7):
  latency    Toxiproxy delays every byte on one dependency
  reset      connections are reset (TCP RST), one direction
  blackhole  bytes in one direction vanish; connections stay open
  crash      SIGKILL a process, or arm a failpoint so it dies at an exact
             line (FAILPOINTS=...=exit) on every pass until disarmed
"""

import random
from dataclasses import asdict, dataclass, field
from typing import Any

# Toxiproxy proxies (verification/toxiproxy.json): every path the system uses
PROXIES = ("api", "postgres", "redis", "kafka", "psp")
NETWORK_FAULTS = ("latency", "reset", "blackhole")
# Containers a scenario may SIGKILL (compose service-index names)
KILLABLE = (
    "api-1",
    "charger-1",
    "sweeper-1",
    "webhook_processor-1",
    "outbox_poller-1",
    "outbox_poller-2",
    "psp-sim-1",
)
# Crash points, and every service whose code passes through them. A crash
# point is armed in all of them at once: the sweeper charges through the
# charger's code, and armed in the charger alone it rarely fired (a
# recreated charger rejoins its consumer group for seconds, and the sweeper
# picks the PENDING payment up first).
FAILPOINTS: dict[str, tuple[str, ...]] = {
    "charger.after_claim": ("charger", "sweeper"),
    "charger.after_provider_call": ("charger", "sweeper"),
    "webhook_processor.before_commit": ("webhook_processor",),
}
# Provider misbehaviour (verification.machine.FAULT_PRESETS) under the
# network faults; "none" keeps some scenarios purely about the network
SIM_PRESETS = ("none", "none", "lost_responses", "declines", "webhook_chaos", "slow")
# Only for the self-test: the provider's webhooks heal a lost response within
# milliseconds, long before the sweeper's retry, so with them delivered the
# planted bug (a fresh key per retry) never gets a retry to break.
WEBHOOKS_LOST = "webhooks_lost"


def sim_profile(preset: str) -> dict[str, object]:
    """The simulator's fault profile (PUT /admin/faults) for a preset."""
    if preset == WEBHOOKS_LOST:
        return {"webhook_drop_rate": 1.0}
    from verification.machine import FAULT_PRESETS

    return dict(FAULT_PRESETS[preset])


@dataclass(frozen=True)
class Event:
    at: float  # seconds from the start of the run
    action: str  # toxic_on | toxic_off | kill | start | failpoint_on | failpoint_off
    target: str  # proxy, container, or services (comma-separated)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    seed: int
    seconds: float
    sim_preset: str
    clients: int
    events: tuple[Event, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        counts: dict[str, int] = {}
        for event in self.events:
            if event.action in ("toxic_on", "kill", "failpoint_on"):
                label = (
                    f"{event.params['kind']}:{event.target}"
                    if event.action == "toxic_on"
                    else f"crash:{event.params['failpoint']}"
                    if event.action == "failpoint_on"
                    else f"{event.action}:{event.target}"
                )
                counts[label] = counts.get(label, 0) + 1
        faults = ", ".join(
            f"{k}x{v}" if v > 1 else k for k, v in sorted(counts.items())
        )
        return f"provider={self.sim_preset}; {faults or 'no faults'}"


def toxic(kind: str, rng: random.Random) -> dict[str, Any]:
    """The Toxiproxy toxic for one network fault.

    Direction matters for writes: "upstream" loses the request (the server
    never saw it), "downstream" loses the response after the server acted,
    which is the case idempotency exists for."""
    stream = rng.choice(("upstream", "downstream"))
    if kind == "latency":
        latency = rng.randint(300, 3_000)
        return {
            "type": "latency",
            "stream": "downstream",
            "attributes": {"latency": latency, "jitter": latency // 5},
        }
    if kind == "reset":
        return {"type": "reset_peer", "stream": stream, "attributes": {"timeout": 0}}
    if kind == "blackhole":
        return {"type": "timeout", "stream": stream, "attributes": {"timeout": 0}}
    raise ValueError(f"unknown network fault {kind!r}")


def build(seed: int, seconds: float = 40.0) -> Scenario:
    if seconds < 15:
        raise ValueError("a scenario needs at least 15 seconds")
    rng = random.Random(seed)
    events: list[Event] = []

    def window(longest: float, shortest: float = 2.0) -> tuple[float, float]:
        start = round(rng.uniform(1.0, seconds - shortest - 4.0), 2)
        length = round(
            rng.uniform(shortest, max(shortest, min(longest, seconds - start - 2.0))),
            2,
        )
        return start, start + length

    for index in range(rng.randint(3, 6)):
        proxy = rng.choice(PROXIES)
        kind = rng.choice(NETWORK_FAULTS)
        name = f"{kind}-{index}"
        start, stop = window(10.0)
        spec = toxic(kind, rng)
        events.append(
            Event(start, "toxic_on", proxy, {"name": name, "kind": kind, **spec})
        )
        events.append(Event(stop, "toxic_off", proxy, {"name": name}))

    for _ in range(rng.randint(1, 3)):
        container = rng.choice(KILLABLE)
        start, stop = window(5.0)
        events.append(Event(start, "kill", container))
        events.append(Event(stop, "start", container))

    if rng.random() < 0.5:
        name = rng.choice(sorted(FAILPOINTS))
        services = ",".join(FAILPOINTS[name])
        # Long enough to outlast the restart it takes to arm it (container
        # start, Kafka group rejoin) and still meet work: shorter windows
        # crashed nothing
        start, stop = window(15.0, shortest=8.0)
        events.append(Event(start, "failpoint_on", services, {"failpoint": name}))
        events.append(Event(stop, "failpoint_off", services))

    events.sort(key=lambda e: (e.at, e.action, e.target))
    return Scenario(
        seed=seed,
        seconds=seconds,
        sim_preset=rng.choice(SIM_PRESETS),
        clients=rng.randint(2, 4),
        events=tuple(events),
    )


def selftest(seconds: float = 20.0) -> Scenario:
    """The fault layer's own test: every provider response is lost for most
    of the run (latency beyond the charger's 2 s timeout) and so is every
    webhook, so only retries can learn a charge's outcome. With the planted
    bug (a fresh provider key per retry) each retry charges again, and the
    checker must say so (I3)."""
    return Scenario(
        seed=-1,
        seconds=seconds,
        sim_preset=WEBHOOKS_LOST,
        clients=2,
        events=(
            Event(
                1.0,
                "toxic_on",
                "psp",
                {
                    "name": "lost-responses",
                    "kind": "latency",
                    "type": "latency",
                    "stream": "downstream",
                    "attributes": {"latency": 3_000, "jitter": 0},
                },
            ),
            Event(seconds - 2.0, "toxic_off", "psp", {"name": "lost-responses"}),
        ),
    )
