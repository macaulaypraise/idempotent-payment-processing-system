# Runbook: Redis down

Redis is a cache of finished responses (ADR 0001). Losing it changes
latency, never correctness: idempotency keys, payments and the ledger are in
Postgres, written in one transaction. **There is no data to recover from
Redis.**

## Symptom

- `GET /health` answers `"status": "degraded", "redis": "error"`.
- `idempotency_cache_errors_total` rising on the API's `/metrics`.
- Replays slower: every retry goes to Postgres instead of the cache.
- Possibly more `409` responses: concurrent duplicates now all wait on the
  key row in Postgres (lock timeout 2 s) instead of some being answered from
  the cache.

## Confirm

```bash
curl -s localhost:8000/health                       # "redis": "error"
docker compose exec redis redis-cli ping            # no PONG
curl -s localhost:8000/metrics | grep -E '^idempotency_cache_errors_total|^payments_created_total'
docker compose ps redis                             # exited? restarting?
docker inspect redis --format '{{.State.OOMKilled}} {{.State.ExitCode}}'
```

Rule out a duplicate risk, so nobody panics: this is 0 whatever Redis does.

```bash
docker compose exec -T postgres psql -U postgres -d payments_db -tAc \
  "SELECT count(*) FROM (SELECT 1 FROM payments GROUP BY tenant_id, idempotency_key HAVING count(*) > 1) d"
```

## Immediate action

1. Start it again: `docker compose start redis`.
2. Nothing else. The API's breaker retries Redis after a 5 s cooldown and
   the cache refills as responses complete. Do not flush Postgres, do not
   restart the API, do not turn idempotency off.
3. If 409s matter to a client, they are retryable by contract
   (`Retry-After: 1`): the client retries with the same key.

## Root-cause checks

- Memory: `OOMKilled true` above, or `redis-cli info memory`. The cache holds
  one response per key for 24 h (`IDEMPOTENCY_TTL_SECONDS`); set
  `maxmemory` with an LRU policy rather than letting it grow.
- Network or DNS between the API and Redis (`REDIS_URL`).
- A slow Redis looks like a dead one: each call has a 250 ms socket timeout
  (`REDIS_SOCKET_TIMEOUT_SECONDS`), then the breaker skips Redis.

## Evidence that this is safe

- `make demo` step 2: 1,000 concurrent requests with one key while Redis is
  stopped: one payment, one provider charge.
- `make load-test-redis-kill`: Locust with Redis stopped for the middle third
  of the run; the script counts duplicate payments in Postgres.
- The state machine runs about half its histories with Redis down.
