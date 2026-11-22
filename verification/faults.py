"""The fault layer's scenario runner (coding companion 7.4 and 7.5).

    make verify-faults                       # FAULT_SCENARIOS seeded scenarios
    python -m verification.faults --seeds 7,12 --seconds 40

Drives the real system (docker-compose.faults.yml: every process in its own
container, every dependency behind Toxiproxy) through seeded scenarios. One
scenario:

  1. reset   empty the database, the provider's store and Redis; heal every
             fault; one API key for the platform tenant
  2. run     client tasks pay, double-submit, refund, transfer and retry
             while the timeline adds and removes network faults, kills and
             restarts processes and arms crash points. Every request is
             recorded (verification.history); a timeout, connection error,
             5xx or 409 is "unknown" and is retried with the same key and
             body, as a correct client would. Safety invariants are sampled
             every few seconds.
  3. heal    remove every fault and restart everything; retry every request
             still unanswered until it is answered (one that never is, on a
             healthy system, is itself a failure)
  4. quiet   wait until no work is left: outbox published, no PENDING or
             leased payment or refund, no unprocessed webhook on either side
  5. check   reconcile every provider file date, then run the same checker
             as the state machine (verification.checker.check_all): I1-I7,
             I9 replays, I10 and the model's rows and balances

On a violation the runner stops and leaves everything in place: the history,
ground truth, scenario and container logs go to .verify-faults/<seed>/, and
the database is left as the checker saw it.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from verification import checker, database
from verification.history import History, Operation
from verification.scenario import Event, Scenario, build, selftest, sim_profile

COMPOSE_FILE = "docker-compose.faults.yml"
PROJECT = "ipps-faults"
CLIENT_TIMEOUT_SECONDS = 5.0
HEAL_SECONDS = 90.0  # to answer every request left unknown, once healthy
QUIET_SECONDS = 180.0  # for the workers to finish everything
SAMPLE_SECONDS = 3.0  # safety invariants during the run


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass(frozen=True)
class Endpoints:
    """How the runner reaches the stack from the host. The API goes through
    Toxiproxy like any client; the rest is the control plane, reached
    directly so resets and checks are never faulted."""

    api: str = _env("FAULTS_API_URL", "http://localhost:58000")
    toxiproxy: str = _env("FAULTS_TOXIPROXY_URL", "http://localhost:58474")
    sim: str = _env("FAULTS_SIM_URL", "http://localhost:58001")
    owner_url: str = _env(
        "FAULTS_OWNER_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:55432/payments_faults",
    )
    redis_url: str = _env("FAULTS_REDIS_URL", "redis://localhost:56379/0")


class ScenarioFailed(Exception):
    def __init__(self, violations: list[str]) -> None:
        super().__init__("\n".join(violations))
        self.violations = violations


# --- docker ----------------------------------------------------------------------


async def _run(*args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode(errors="replace")


async def compose(*args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    return await _run("docker", "compose", "-f", COMPOSE_FILE, *args, env=env)


def container(name: str) -> str:
    """'charger-1' -> 'ipps-faults-charger-1'"""
    return f"{PROJECT}-{name}"


async def recreate(*services: str, env: dict[str, str] | None = None) -> None:
    """Replace containers so a new environment (a failpoint, a planted bug)
    takes effect. Compose reads ${SERVICE}_FAILPOINTS and ${PLANTED_BUGS}.

    The old container is killed at once (--timeout 0). A graceful stop
    takes Docker's full 10 s grace period: a worker is PID 1 in its
    container with no SIGTERM handler, and the kernel ignores signals to
    PID 1 that it has no handler for (backlog S7-7).
    With the wait, a crash window was over before its containers were up,
    and crashed nothing."""
    code, output = await compose(
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--force-recreate",
        "--timeout",
        "0",
        *services,
        env=env,
    )
    if code != 0:
        raise RuntimeError(f"recreating {services} failed:\n{output}")


async def restarts(name: str) -> int:
    """How often Docker restarted a container (each failpoint crash is one)."""
    code, output = await _run(
        "docker", "inspect", "--format", "{{.RestartCount}}", container(name)
    )
    return int(output.strip()) if code == 0 and output.strip().isdigit() else 0


# --- toxiproxy -------------------------------------------------------------------


class Toxiproxy:
    """The few calls the runner needs from Toxiproxy's HTTP API."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def heal(self) -> None:
        """Remove every toxic and re-enable every proxy."""
        (await self.http.post("/reset")).raise_for_status()

    async def add(self, proxy: str, name: str, spec: dict[str, Any]) -> None:
        body = {
            "name": name,
            "type": spec["type"],
            "stream": spec["stream"],
            "toxicity": 1.0,
            "attributes": spec["attributes"],
        }
        response = await self.http.post(f"/proxies/{proxy}/toxics", json=body)
        response.raise_for_status()

    async def remove(self, proxy: str, name: str) -> None:
        response = await self.http.delete(f"/proxies/{proxy}/toxics/{name}")
        if response.status_code != 404:
            response.raise_for_status()


# --- the client --------------------------------------------------------------------


@dataclass
class Request:
    op: str
    path: str
    key: str
    body: dict[str, Any]


class TimedHistory(History):
    """A history whose operations carry the seconds since the run started."""

    def __init__(self) -> None:
        super().__init__()
        self.t0 = time.monotonic()

    def record(self, *args: Any, **kwargs: Any) -> Operation:
        kwargs.setdefault("at", round(time.monotonic() - self.t0, 3))
        return super().record(*args, **kwargs)


@dataclass
class Client:
    """A correct client: every request carries an idempotency key, and an
    unknown outcome is retried with the same key and body until answered."""

    http: httpx.AsyncClient
    headers: dict[str, str]
    history: History
    stats: Counter[str]
    wallets: list[str] = field(default_factory=list)
    payments: list[tuple[Request, dict[str, Any]]] = field(default_factory=list)
    unanswered: list[Request] = field(default_factory=list)

    async def send(self, request: Request) -> tuple[str, int | None, Any]:
        try:
            response = await self.http.post(
                request.path,
                json=request.body,
                headers={**self.headers, "Idempotency-Key": request.key},
            )
        except httpx.HTTPError as exc:
            op = self.history.record(
                "client",
                request.op,
                key=request.key,
                request=request.body,
                body=f"{type(exc).__name__}: {exc}",
            )
            self.stats["client unknown"] += 1
            return op.outcome, None, None
        try:
            answer: Any = response.json()
        except ValueError:
            answer = response.text
        op = self.history.record(
            "client",
            request.op,
            key=request.key,
            request=request.body,
            status=response.status_code,
            body=answer,
        )
        self.stats[f"{request.op} {response.status_code}"] += 1
        if op.outcome == "unknown":
            self.stats["client unknown"] += 1
        return op.outcome, response.status_code, answer

    async def until_answered(self, request: Request, deadline: float) -> Any:
        """The answer body for a 2xx, None for a 4xx or no answer by the
        deadline (then the request is kept to retry after healing)."""
        delay = 0.2
        while True:
            outcome, _, answer = await self.send(request)
            if outcome == "ok":
                return answer
            if outcome == "fail":
                return None
            if time.monotonic() >= deadline:
                self.unanswered.append(request)
                return None
            self.stats["client retries"] += 1
            await asyncio.sleep(random.uniform(0, delay))
            delay = min(delay * 2, 2.0)

    # --- operations ---------------------------------------------------------------

    async def create_wallet(self, deadline: float) -> None:
        request = Request("create_wallet", "/v1/wallets", _key(), {"currency": "USD"})
        answer = await self.until_answered(request, deadline)
        if answer is not None:
            self.wallets.append(answer["wallet_id"])

    def _payment(self, rng: random.Random) -> Request:
        body = {
            "amount": rng.randint(100, 5_000),
            "currency": "USD",
            "wallet_id": rng.choice(self.wallets),
        }
        return Request("create_payment", "/v1/payments", _key(), body)

    async def pay(self, rng: random.Random, deadline: float) -> None:
        request = self._payment(rng)
        answer = await self.until_answered(request, deadline)
        if answer is not None:
            self.payments.append((request, answer))

    async def double_submit(self, rng: random.Random, deadline: float) -> None:
        """The same request twice at once (a double click): one payment."""
        request = self._payment(rng)
        answers = await asyncio.gather(
            self.until_answered(request, deadline),
            self.until_answered(request, deadline),
        )
        # until_answered may have kept it twice; once is enough to retry
        while self.unanswered.count(request) > 1:
            self.unanswered.remove(request)
        answer = next((a for a in answers if a is not None), None)
        if answer is not None:
            self.payments.append((request, answer))

    async def other_body(self, rng: random.Random, deadline: float) -> None:
        """An answered key reused with a different body: must be 422 (I9)."""
        if not self.payments:
            return
        original, _ = rng.choice(self.payments)
        body = {**original.body, "amount": original.body["amount"] + 1}
        await self.until_answered(
            Request(original.op, original.path, original.key, body), deadline
        )

    async def refund(self, rng: random.Random, deadline: float) -> None:
        if not self.payments:
            return
        _, answer = rng.choice(self.payments)
        request = Request(
            "create_refund", "/v1/refunds", _key(), {"payment_id": answer["payment_id"]}
        )
        await self.until_answered(request, deadline)

    async def transfer(self, rng: random.Random, deadline: float) -> None:
        if len(self.wallets) < 2:
            return
        source, target = rng.sample(self.wallets, 2)
        body = {
            "from_wallet_id": source,
            "to_wallet_id": target,
            "amount": rng.randint(1, 2_000),
        }
        await self.until_answered(
            Request("create_transfer", "/v1/transfers", _key(), body), deadline
        )

    async def work(self, rng: random.Random, until: float) -> None:
        operations = (
            (0.40, self.pay),
            (0.55, self.double_submit),
            (0.70, self.refund),
            (0.85, self.transfer),
            (1.00, self.other_body),
        )
        while time.monotonic() < until:
            roll = rng.random()
            operation = next(op for limit, op in operations if roll < limit)
            await operation(rng, until)
            await asyncio.sleep(rng.uniform(0.05, 0.3))

    async def answer_the_rest(self, deadline: float) -> list[str]:
        """After healing, every unknown request must get an answer."""
        pending, self.unanswered = self.unanswered, []
        for request in pending:
            answer = await self.until_answered(request, deadline)
            if answer is not None and request.op == "create_payment":
                self.payments.append((request, answer))
        return [
            f"availability: {r.op} key {r.key} still unanswered "
            f"{HEAL_SECONDS:.0f}s after every fault was removed"
            for r in self.unanswered
        ]


def _key() -> str:
    return str(uuid.uuid4())


# --- one scenario --------------------------------------------------------------------


class Runner:
    def __init__(self, endpoints: Endpoints, out_dir: Path) -> None:
        self.endpoints = endpoints
        self.out_dir = out_dir
        self.stats: Counter[str] = Counter()
        self.engine = create_async_engine(endpoints.owner_url, pool_size=4)
        self.factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self.sim = httpx.AsyncClient(base_url=endpoints.sim, timeout=10.0)
        self.toxiproxy = Toxiproxy(
            httpx.AsyncClient(base_url=endpoints.toxiproxy, timeout=10.0)
        )
        self.down: set[str] = set()  # containers killed and not yet restarted
        self.armed: set[str] = set()  # services with a failpoint armed

    async def close(self) -> None:
        await self.sim.aclose()
        await self.toxiproxy.http.aclose()
        await self.engine.dispose()

    # --- stack control ----------------------------------------------------------

    async def wait_healthy(self, seconds: float = 120.0) -> None:
        deadline = time.monotonic() + seconds
        last = "no answer"
        async with httpx.AsyncClient(timeout=3.0) as http:
            while time.monotonic() < deadline:
                try:
                    api = await http.get(f"{self.endpoints.api}/health")
                    sim = await http.get(f"{self.endpoints.sim}/health")
                    last = (
                        f"api {api.status_code} {api.text[:120]}; sim {sim.status_code}"
                    )
                    if api.status_code == 200 and sim.status_code == 200:
                        if api.json().get("status") == "ok":
                            return
                except httpx.HTTPError as exc:
                    last = repr(exc)
                await asyncio.sleep(1.0)
        raise RuntimeError(f"the fault stack is not healthy: {last}")

    async def heal(self) -> None:
        await self.toxiproxy.heal()
        if self.armed:
            await recreate(*sorted(self.armed))
            self.armed.clear()
        for name in sorted(self.down):
            await _run("docker", "start", container(name))
        self.down.clear()
        await self.wait_healthy()
        (await self.sim.put("/admin/faults", json={})).raise_for_status()

    async def reset(self) -> dict[str, str]:
        from app.cli import create_api_key
        from app.models.tenant import DEFAULT_TENANT_ID

        await self.heal()
        (await self.sim.post("/admin/reset")).raise_for_status()
        await database.reset(self.endpoints.owner_url)
        import redis.asyncio as redis

        client = redis.from_url(self.endpoints.redis_url)
        try:
            await client.flushdb()
        finally:
            await client.aclose()
        async with self.factory() as db:
            api_key = await create_api_key(db, DEFAULT_TENANT_ID)
        return {"Authorization": f"Bearer {api_key}"}

    async def apply(self, event: Event, history: History) -> None:
        """One timeline event. Docker errors (killing a container that is
        already down, say) are recorded, not fatal."""
        detail: Any = None
        if event.action == "toxic_on":
            await self.toxiproxy.add(event.target, event.params["name"], event.params)
        elif event.action == "toxic_off":
            await self.toxiproxy.remove(event.target, event.params["name"])
        elif event.action == "kill":
            _, detail = await _run(
                "docker", "kill", "--signal", "KILL", container(event.target)
            )
            self.down.add(event.target)
        elif event.action == "start":
            _, detail = await _run("docker", "start", container(event.target))
            self.down.discard(event.target)
        elif event.action == "failpoint_on":
            services = event.target.split(",")
            armed = f"{event.params['failpoint']}=exit"
            await recreate(
                *services, env={f"{s.upper()}_FAILPOINTS": armed for s in services}
            )
            self.armed.update(services)
        elif event.action == "failpoint_off":
            services = event.target.split(",")
            crashes = sum([await restarts(f"{s}-1") for s in services])
            self.stats["failpoint crashes"] += crashes
            detail = f"{crashes} crashes"
            await recreate(*services)
            self.armed.difference_update(services)
        self.stats[f"fault {event.action} {event.target}"] += 1
        history.record(
            "fault",
            event.action,
            request={"target": event.target, **event.params},
            body=detail.strip() if isinstance(detail, str) else None,
            outcome="ok",
        )

    async def timeline(self, scenario: Scenario, history: History, t0: float) -> None:
        for event in scenario.events:
            await asyncio.sleep(max(0.0, t0 + event.at - time.monotonic()))
            await self.apply(event, history)

    # --- checks -----------------------------------------------------------------

    async def ground_truth(self) -> dict[str, Any]:
        response = await self.sim.get("/ground-truth")
        response.raise_for_status()
        return dict(response.json())

    async def sample_safety(self, history: History, until: float) -> list[str]:
        """Safety holds at every instant; sample it while faults are live."""
        found: list[str] = []
        while time.monotonic() < until:
            await asyncio.sleep(SAMPLE_SECONDS)
            try:
                truth = await self.ground_truth()
            except httpx.HTTPError:
                continue  # the simulator may be the process that was killed
            async with self.factory() as db:
                violations = await checker.safety(db, truth)
            violations += checker.replays(history)
            self.stats["safety samples"] += 1
            for violation in violations:
                if violation not in found:
                    found.append(violation)
        return found

    async def wait_quiet(self) -> list[str]:
        """Until no work is left, unchanged for three polls in a row."""
        query = text(
            """
            SELECT
              (SELECT count(*) FROM outbox_events WHERE published_at IS NULL),
              (SELECT count(*) FROM payments WHERE status = 'PENDING'
                 OR (status = 'PROCESSING' AND next_attempt_at IS NOT NULL)),
              (SELECT count(*) FROM refunds WHERE status = 'PENDING'
                 OR (status = 'PROCESSING' AND next_attempt_at IS NOT NULL)),
              (SELECT count(*) FROM webhook_events
                 WHERE status IN ('received', 'parked'))
            """
        )
        deadline = time.monotonic() + QUIET_SECONDS
        quiet_polls = 0
        work: tuple[int, ...] = ()
        while time.monotonic() < deadline:
            # Deliver the provider's pending webhooks now rather than at
            # their (possibly far) retry time
            await self.sim.post("/admin/webhooks/drain")
            pending = (await self.sim.get("/admin/webhooks?status=pending")).json()
            async with self.factory() as db:
                work = tuple((await db.execute(query)).one()) + (len(pending),)
            quiet_polls = quiet_polls + 1 if not any(work) else 0
            if quiet_polls >= 3:
                return []
            await asyncio.sleep(1.0)
        labels = ("unpublished outbox", "payments", "refunds", "webhooks", "sim")
        left = ", ".join(f"{n} {label}" for n, label in zip(work, labels, strict=True))
        return [f"liveness: not quiet {QUIET_SECONDS:.0f}s after healing ({left})"]

    async def reconcile(self, headers: dict[str, str], truth: dict[str, Any]) -> None:
        days = {
            date.fromisoformat(row["created_at"][:10])
            for row in truth["charges"] + truth["refunds"]
        } | {datetime.now(UTC).date()}
        async with httpx.AsyncClient(base_url=self.endpoints.api, timeout=60.0) as api:
            for day in sorted(days):
                for _ in range(5):
                    response = await api.post(
                        "/v1/reconciliations",
                        json={"date": day.isoformat()},
                        headers=headers,
                    )
                    if response.status_code == 201:
                        self.stats["reconciliation runs"] += 1
                        break
                    await asyncio.sleep(2.0)
                else:
                    raise RuntimeError(
                        f"reconciliation of {day} failed: "
                        f"{response.status_code} {response.text}"
                    )

    # --- the scenario -------------------------------------------------------------

    async def run(self, scenario: Scenario) -> dict[str, Any]:
        history = TimedHistory()
        started = time.monotonic()
        crashes_before = self.stats["failpoint crashes"]
        headers = await self.reset()
        if scenario.sim_preset != "none":
            profile = {**sim_profile(scenario.sim_preset), "seed": scenario.seed}
            (await self.sim.put("/admin/faults", json=profile)).raise_for_status()
            history.record("fault", "set_faults", request=profile, outcome="ok")

        async with httpx.AsyncClient(
            base_url=self.endpoints.api, timeout=CLIENT_TIMEOUT_SECONDS
        ) as http:
            client = Client(http, headers, history, self.stats)
            setup_deadline = time.monotonic() + 30.0
            for _ in range(3):
                await client.create_wallet(setup_deadline)
            if len(client.wallets) < 3:
                raise RuntimeError("could not create wallets on a healthy stack")

            t0 = time.monotonic()
            history.t0 = t0  # the timeline's second 0; setup is negative
            until = t0 + scenario.seconds
            timeline = asyncio.create_task(self.timeline(scenario, history, t0))
            sampler = asyncio.create_task(self.sample_safety(history, until))
            await asyncio.gather(
                *[
                    client.work(random.Random(f"{scenario.seed}/{i}"), until)
                    for i in range(scenario.clients)
                ]
            )
            await timeline
            violations = await sampler

            await self.heal()
            violations += await client.answer_the_rest(time.monotonic() + HEAL_SECONDS)
        violations += await self.wait_quiet()
        quiet_at = time.monotonic()

        truth = await self.ground_truth()
        if not any(v.startswith("liveness") for v in violations):
            await self.reconcile(headers, truth)
            truth = await self.ground_truth()
        async with self.factory() as db:
            violations += await checker.check_all(db, history, truth)
            by_status = text("SELECT status, count(*) FROM payments GROUP BY 1")
            statuses = (await db.execute(by_status)).all()

        self.stats["scenarios"] += 1
        self.stats["client operations"] += sum(1 for _ in history.client())
        self.stats["provider charges"] += len(truth["charges"])
        self.stats["provider refunds"] += len(truth["refunds"])
        for status, count in statuses:
            self.stats[f"payments ending {status}"] += count
        summary = {
            "seed": scenario.seed,
            "plan": scenario.describe(),
            "requests": sum(1 for _ in history.client()),
            "unknown": sum(1 for o in history.client() if o.outcome == "unknown"),
            "charges": len(truth["charges"]),
            "kills": sum(1 for e in scenario.events if e.action == "kill"),
            "crashes": self.stats["failpoint crashes"] - crashes_before,
            "quiet_after_s": round(quiet_at - t0 - scenario.seconds, 1),
            "total_s": round(time.monotonic() - started, 1),
        }
        if violations:
            await self.save(scenario, history, truth)
            raise ScenarioFailed(violations)
        return summary

    async def save(
        self, scenario: Scenario, history: History, truth: dict[str, Any]
    ) -> Path:
        directory = self.out_dir / str(scenario.seed)
        directory.mkdir(parents=True, exist_ok=True)
        history.dump(directory / "history.jsonl")
        (directory / "ground-truth.json").write_text(json.dumps(truth, default=str))
        (directory / "scenario.json").write_text(
            json.dumps(scenario.as_dict(), indent=2, default=str)
        )
        _, logs = await compose("logs", "--no-color", "--timestamps")
        (directory / "compose.log").write_text(logs)
        return directory


# --- the self-test -------------------------------------------------------------------


async def run_selftest(runner: Runner) -> bool:
    """With a bug planted in the charger and sweeper, the runner must fail."""
    await recreate("charger", "sweeper", env={"PLANTED_BUGS": "fresh_provider_key"})
    try:
        await runner.run(selftest())
    except ScenarioFailed as failure:
        caught = any(v.startswith("I3") for v in failure.violations)
        print(
            f"fault self-test: planted bug {'caught' if caught else 'NOT caught'} "
            f"({len(failure.violations)} violations, first: {failure.violations[0]})"
        )
        return caught
    finally:
        await recreate("charger", "sweeper")
    print("fault self-test: planted bug NOT caught (no violation)")
    return False


# --- entry point ---------------------------------------------------------------------


def _seeds(args: argparse.Namespace) -> list[int]:
    if args.seeds:
        return [int(s) for s in args.seeds.split(",") if s.strip()]
    base = args.seed
    if base is None:
        base = random.SystemRandom().randrange(10**6)
    return [base + i for i in range(args.scenarios)]


async def main_async(args: argparse.Namespace) -> int:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL)
    )
    runner = Runner(Endpoints(), Path(args.out))
    try:
        await runner.wait_healthy(args.startup_seconds)
        if args.selftest:
            if not await run_selftest(runner):
                return 1
            runner.stats.clear()  # the totals are the scenarios' alone
        seeds = _seeds(args)
        print(f"faults: {len(seeds)} scenarios of {args.seconds:.0f}s, seeds {seeds}")
        for seed in seeds:
            scenario = build(seed, args.seconds)
            print(f"scenario {seed}: {scenario.describe()}", flush=True)
            try:
                summary = await runner.run(scenario)
            except ScenarioFailed as failure:
                print(f"scenario {seed}: FAILED")
                for violation in failure.violations:
                    print(f"  {violation}")
                print(
                    f"  history, ground truth and logs: {args.out}/{seed}/\n"
                    f"  re-run the same plan: python -m verification.faults "
                    f"--seeds {seed} --seconds {args.seconds:.0f}"
                )
                return 1
            print(
                f"scenario {seed}: ok - {summary['requests']} requests "
                f"({summary['unknown']} unknown), {summary['charges']} charges, "
                f"{summary['kills']} kills, {summary['crashes']} failpoint crashes, "
                f"quiet {summary['quiet_after_s']}s after the run, "
                f"{summary['total_s']}s total",
                flush=True,
            )
        print(f"faults: {len(seeds)} scenarios, 0 violations")
        for name, count in sorted(runner.stats.items()):
            print(f"  {name}: {count}")
        return 0
    finally:
        with contextlib.suppress(Exception):
            await runner.close()


def main() -> None:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m verification.faults")
    parser.add_argument(
        "--scenarios", type=int, default=int(_env("FAULT_SCENARIOS", "8"))
    )
    parser.add_argument(
        "--seconds", type=float, default=float(_env("FAULT_SECONDS", "40"))
    )
    parser.add_argument("--seed", type=int, default=None, help="first seed")
    parser.add_argument("--seeds", default=_env("FAULT_SEEDS", ""), help="e.g. 7,12")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--out", default=".verify-faults")
    parser.add_argument("--startup-seconds", type=float, default=180.0)
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
