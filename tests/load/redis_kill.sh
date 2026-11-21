#!/usr/bin/env bash
# Stage 1 exit gate: run Locust, stop Redis for the middle third of the run,
# then report client-side errors, duplicates seen by clients, and duplicate
# payments in Postgres.
#
#   export IPPS_API_KEY=...            # make tenant NAME=loadtest
#   tests/load/redis_kill.sh           # USERS=200 DURATION=90 by default
#
# Needs the stack from `make dev` (api, postgres, redis) on this machine.
set -euo pipefail

USERS="${USERS:-200}"
DURATION="${DURATION:-90}"
HOST="${HOST:-http://localhost:8000}"
PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-payments_db}"
OUT="${OUT:-tests/load/results/redis-kill-$(date +%Y%m%d-%H%M%S)}"
: "${IPPS_API_KEY:?set IPPS_API_KEY (make tenant NAME=loadtest)}"

mkdir -p "$(dirname "$OUT")"
psql() { docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tA -c "$1"; }
before=$(psql "SELECT count(*) FROM payments")

echo ">>> locust: ${USERS} users for ${DURATION}s against ${HOST}"
poetry run locust -f tests/load/locustfile.py --headless \
  -u "$USERS" -r "$USERS" -t "${DURATION}s" --host "$HOST" \
  --csv "$OUT" --only-summary > "$OUT.log" 2>&1 &
locust_pid=$!

third=$((DURATION / 3))
sleep "$third"
echo ">>> $(date +%T) stopping redis";  docker compose stop redis > /dev/null
redis_down_at=$(date +%s)
sleep "$third"
echo ">>> $(date +%T) starting redis";  docker compose start redis > /dev/null
redis_down_for=$(( $(date +%s) - redis_down_at ))
wait "$locust_pid" || true  # locust exits non-zero on any failure; reported below

after=$(psql "SELECT count(*) FROM payments")
dupe_keys=$(psql "SELECT count(*) FROM (SELECT 1 FROM payments GROUP BY tenant_id, idempotency_key HAVING count(*) > 1) d")

python3 - "$OUT" "$redis_down_for" "$((after - before))" "$dupe_keys" <<'PY'
import csv, sys
out, down, created, dupe_keys = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
stats = list(csv.DictReader(open(f"{out}_stats.csv")))
total = next(r for r in stats if r["Name"] == "Aggregated")
failures = list(csv.DictReader(open(f"{out}_failures.csv")))
dupes = sum(int(f["Occurrences"]) for f in failures if "DUPLICATE" in f["Error"])
codes = {}
for f in failures:
    if "DUPLICATE" in f["Error"]:
        continue
    reason = f["Error"][:80]
    codes[reason] = codes.get(reason, 0) + int(f["Occurrences"])
print()
print(f"Redis down for:            {down}s of the run")
print(f"Requests:                  {total['Request Count']}")
print(f"Failed requests:           {total['Failure Count']}")
for reason, n in sorted(codes.items(), key=lambda kv: -kv[1]):
    print(f"    {n:>6}  {reason}")
print(f"p50 / p99 latency (ms):    {total['50%']} / {total['99%']}")
print(f"Payments created:          {created}")
print(f"Client-seen duplicates:    {dupes}")
print(f"Keys with >1 payment (DB): {dupe_keys}")
print(f"CSV: {out}_stats.csv  log: {out}.log")
sys.exit(1 if dupes or dupe_keys else 0)
PY
