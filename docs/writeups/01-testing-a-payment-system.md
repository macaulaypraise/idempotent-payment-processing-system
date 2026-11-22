# Testing a payment system with generated histories and fault injection

*Draft, 2026-11-22. Source: [docs/verification.md](../verification.md), where every claim below has
a command and a log entry.*

The first version of this project proved itself with a load test: 1,000
concurrent users, about 12,700 requests, zero duplicate charges. That number
is true, and it proves less than it seems to. A load test sends the requests
someone thought of, through a network that behaves. The failures that cost
money in payments are a provider that charges you and then times out, a
webhook that arrives twice or before the event it depends on, a worker that
dies between "the provider said yes" and "we wrote it down". None of these
shows up in a load test, because none of them is load.

So for v2 I set myself a different bar: not "the tests pass", but "these
invariants held across N adversarial histories, and the harness can prove it
would have noticed if they had not".

## The invariants

The contract is ten statements about the database and the provider's
records. Every transfer balances. The whole ledger balances. One payment
per idempotency key, and, more importantly, one charge at the provider per
payment. Cached account totals equal the sum of their entries. No wallet
goes negative. After reconciliation, the clearing account is zero. Every
acknowledged write exists, and every answer under one key is identical.

Most of these are single SQL queries. The interesting one is "one charge at
the provider per payment", because it needs a source of truth outside the
system. The provider simulator keeps its own SQLite record of every charge
it made. The checker asks the provider, not our database, how many times
each payment was charged.

## Layer one: a state machine over the whole system

The first layer is a Hypothesis `RuleBasedStateMachine`. Its rules are
everything that can happen, in any order: a client creates a payment, retries
it, retries it with a different body, refunds it, transfers between wallets.
A worker publishes one outbox batch, consumes one queued event (or leaves it
queued to be delivered again), sweeps with the clock moved forward, delivers
one pending webhook (or delivers it twice), runs reconciliation. The provider
switches between behaving, declining, losing responses after charging,
hanging, and scrambling its webhooks. Redis is up for some histories and down
for others.

All of it runs in one process against real Postgres: the API through httpx's
ASGI transport, the simulator the same way, the workers through their
single-step functions. That makes a history about a hundred milliseconds of
work per step, so a nightly run covers a few hundred histories and about ten
thousand steps.

After every step, the safety invariants must hold. At the end, and whenever
a "quiesce" rule fires, every worker runs until nothing changes, everything
is reconciled, and the end state is compared with a model.

The model was the part I got wrong first. An asynchronous system has no
single expected state after a step: a payment might be pending, processing or
captured, depending on which worker ran. So the model never predicts. It is
rebuilt from the client's history alone (what was acknowledged, each write
once however often it was retried) and, once the system is quiet, it asks the
provider what actually happened. Charged means the payment must be settled
or refunded. Declined means failed. Never charged means it is still
processing, dead-lettered. And every wallet's balance must equal, to the
minor unit, the charges minus the refunds plus the transfers in minus the
transfers out. That one line ties together three independent sources: the
ledger, the provider and the client.

## A harness that cannot catch a bug proves nothing

The second thing I built was a way to make the harness fail on purpose. A
switch, impossible to turn on in production, makes the charger send a fresh
provider key on every retry. That is the classic mistake: a retry after a
lost response charges the customer again. The harness must find it, in every
CI run.

It did not, at first. It missed it three times, and each miss was a real
weakness in the harness:

- **The workers always ran in the same order** in the quiesce step: webhooks
  first, then retries. The provider's webhook told us about the lost charge
  before any retry could happen, so the buggy retry never ran. The order is
  now drawn per history.
- **Redis was always up**, so a second planted bug (no fingerprint check on
  the database replay path) was hidden: every replay came from the cache.
  Redis is now up or down per history.
- **Faults were cleared before in-flight work met them.** Quiesce turned the
  provider back to normal and then drained the workers, so pending payments
  never met a faulty provider. Quiesce now runs one pass under the faults
  first.

The general lesson: defence in depth is good for the system and bad for the
test. Every healing path (webhooks, the sweeper, reconciliation) is also a
way for a bug to become invisible.

## Making histories replayable

Hypothesis's power is that it replays a failing history and shrinks it to a
handful of steps. That only works if the same choices always produce the
same behaviour. Mine did not, and Hypothesis said so with
`FlakyStrategyDefinition` after 384 histories on my laptop.

Five things outside Hypothesis's control had been deciding outcomes: worker
random seeds taken from a run-wide counter, the sweeper and the webhook
processor running items concurrently (so the simulator's ordered fault draws
met requests in scheduler order), a client timeout racing in-process calls
under load, and a wall-clock comparison near the sweeper's grace period.
The fix was to take every one of them out of the outcome. Seeds are fixed or
drawn by Hypothesis. Harness workers run one item at a time. And no wall
clock decides whether a response is lost: the simulator raises `TimeoutError`
at the moment the response would go missing, after the charge is recorded.
My first attempt, a longer timeout, still flaked under load. Only removing
the clock held.

That fix cost something real, and the docs say so: the state machine no
longer exercises concurrent sweeps. The second layer exists for that.

## The bug in the product

After the fix the nightly job on GitHub failed again, after 196 clean
histories, in a way I could not reproduce locally. Replaying GitHub's exact
histories in a loaded sandbox reproduced it after 15.

The cause was in the product. The sweeper claimed due payments with
`UPDATE … WHERE id IN (SELECT … ORDER BY due LIMIT n) RETURNING …` and
attempted them in the order returned. Postgres does not promise that
`RETURNING` follows the subquery's `ORDER BY`. It returns rows in physical
order, which moves with updates and vacuum. "Oldest due first" had never
been true. No money was at risk, since any order is safe, but it is a real
bug, it was invisible to every test I had written, and the harness found it
because it is sensitive to order by construction. The claim is now a CTE that
returns rows explicitly ordered, with a regression test that lays out the
heap in reverse and fails on the old query three times out of three.

The same investigation found that Hypothesis silently switches to a
derandomized profile when the `CI` variable is set. My nightly profile
inherited it, so every night would have replayed the same 300 histories:
"five green nights" would have been one run five times over.

## Layer two: the real system, behind Toxiproxy

The state machine cannot see races, real Kafka, or real time. The fault
layer runs the system as it is deployed: every process in its own container,
two outbox relays, and Toxiproxy between every process and every dependency,
including between the client and the API. Kafka advertises the proxy's
address, or clients would go around it after their first metadata request.

A scenario is a seed that expands into a forty-second plan: latency, resets
and blackholes on Postgres, Redis, Kafka, the provider or the API, in the
request or the response direction; SIGKILLs with restarts; crash points
armed so a worker dies at an exact line, such as after the provider charged
and before the commit. Concurrent clients pay, double-submit, refund and
transfer, and treat every timeout, 5xx or 409 as "unknown": they retry with
the same key and body, as a correct client would. Afterwards every fault is
removed, every unknown request must get an answer, the workers must finish,
and the same checker runs.

This layer found two problems in the system and three in itself:

- **Database outages surfaced as bare 500s.** A connection reset mid-request
  escaped as an unhandled exception. The outcome of such a request is
  unknown, which is exactly what 503 with `Retry-After` says; it now does,
  and only for connection failures, so a broken query is still a 500.
- **Workers ignored SIGTERM.** Each worker is PID 1 in its container, and
  the kernel delivers no signal to PID 1 that it has no handler for. Every
  stop and every deploy waited out Docker's whole grace period and ended in
  SIGKILL. The system is crash-safe, so nothing was lost, but deploys were
  slow. Workers now finish the item in hand and leave cleanly.
- In the harness: the simulator's store was on tmpfs, so killing it erased
  the provider's memory of every charge (the checker objected, correctly);
  the fault self-test was masked by webhooks, again; and the crash windows
  crashed nothing, which I only noticed because I had added a counter.

That last one is the moral of the whole project. Every scenario with a crash
point passed. The counter read zero. Arming a crash point meant replacing the
container, the replacement waited ten seconds for the old one to ignore its
SIGTERM, and by then the window was over. A green run with no crashes in it
is not evidence of crash safety. The summary now prints crashes per
scenario, so that number can never silently go back to zero.

## Results, and what they do not mean

On my laptop, the nightly configuration ran 361 histories (9,460 steps,
1,140 provider charges) and 8 fault scenarios (3,462 requests, 41 network
faults, 17 kills, 3 crashes mid-charge, 1,526 provider charges for 1,526
payments) with no violation. Both run every night in CI; both self-tests
catch their planted bug every time.

That is not a proof. It says no violation happened in the histories that
were explored. The fault layer reproduces a plan, not an interleaving, so a
failure there may not recur on a re-run. Neither layer injects network
partitions between two services, or clock skew. And the provider is a
simulator I wrote, so it misbehaves only in the ways I thought of. Those
limits are written down next to the results, which is the point: a reviewer
can see exactly what was checked, rerun it, and decide how much it is worth.

What changed for me is the question I ask of a test. Not "does it pass", but
"what would make it fail, and have I seen it fail for that reason".
