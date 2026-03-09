# Deployment Guide — Railway.app

## Prerequisites
- GitHub repo pushed and CI passing
- Railway account at https://railway.app (free tier is enough)
- Railway CLI: `npm install -g @railway/cli`

---

## Step 1 — Create the Railway project

```bash
railway login
railway init   # creates a new project, select "Empty project"
```

Or do it in the browser: railway.app → New Project → Empty Project.

---

## Step 2 — Add PostgreSQL

In the Railway dashboard:
1. Click **+ New** → **Database** → **PostgreSQL**
2. Railway provisions the DB and auto-sets `DATABASE_URL` in your project environment

Verify it's there: **Variables** tab → you should see `DATABASE_URL` already injected.

---

## Step 3 — Add Redis

1. Click **+ New** → **Database** → **Redis**
2. Railway auto-sets `REDIS_URL`

---

## Step 4 — Add Kafka (Upstash)

Railway doesn't have a native Kafka add-on. Use Upstash (free tier):

1. Go to https://console.upstash.com → Create Kafka cluster → choose the region closest to your Railway deployment region
2. Create these topics:
   - `payment.initiated`
   - `payment.completed`
   - `payment.failed`
   - `payment.dlq`
3. Copy the **Bootstrap Server** URL from the Upstash dashboard

---

## Step 5 — Deploy the app

```bash
# Link your local repo to the Railway project
railway link

# Deploy
railway up
```

Or connect GitHub for automatic deploys:
Dashboard → your service → **Settings** → **Connect GitHub repo** → select your repo → deploy on push to `main`.

---

## Step 6 — Set environment variables

In the Railway dashboard → your app service → **Variables** tab, add:

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `KAFKA_BOOTSTRAP_SERVERS` | paste from Upstash dashboard |
| `KAFKA_TOPIC_PAYMENT_INITIATED` | `payment.initiated` |
| `KAFKA_TOPIC_PAYMENT_COMPLETED` | `payment.completed` |
| `KAFKA_TOPIC_PAYMENT_FAILED` | `payment.failed` |
| `KAFKA_TOPIC_DLQ` | `payment.dlq` |
| `KAFKA_CONSUMER_GROUP` | `settlement-worker` |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` |
| `IDEMPOTENCY_LOCK_TTL_SECONDS` | `30` |
| `PAYMENT_PROVIDER_URL` | `https://your-mock-provider-url/charge` |
| `PAYMENT_MAX_RETRIES` | `5` |
| `LOG_LEVEL` | `INFO` |
| `SERVICE_NAME` | `ipps-payment-api` |

`DATABASE_URL` and `REDIS_URL` are already injected by Railway — do not add them manually.

---

## Step 7 — Run database migrations

Railway provides a one-off command runner:

```bash
railway run alembic upgrade head
```

Or via the dashboard: your service → **Settings** → **Deploy** → add to the deploy command:
```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

---

## Step 8 — Deploy workers as separate services

The outbox poller and settlement worker run as separate Railway services using the same Docker image but a different start command.

For each worker:
1. Dashboard → **+ New** → **GitHub Repo** → same repo
2. **Settings** → **Build** → Dockerfile path: `Dockerfile`
3. **Settings** → **Deploy** → Start command:

| Service name | Start command |
|---|---|
| `outbox-poller` | `python -m app.workers.outbox_poller` |
| `settlement-worker` | `python -m app.workers.settlement_worker` |

4. Add the same environment variables from Step 6 to each worker service

---

## Step 9 — Verify deployment

```bash
# Get your public URL
railway domain

# Check health
curl https://<your-domain>/health

# Expected:
# {"status": "ok", "redis": "ok", "postgres": "ok", "kafka": "ok"}
```

Then run through the checklist:

```bash
# 1. Create a payment
KEY=$(uuidgen)
curl -X POST https://<your-domain>/payments \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount": 99.99, "currency": "USD"}'

# 2. Retry — must return identical payment_id
curl -X POST https://<your-domain>/payments \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount": 99.99, "currency": "USD"}'

# 3. Check Prometheus metrics are flowing
curl https://<your-domain>/metrics | grep payments_created_total
```

---

## Step 10 — Add your live URL to the README

Once verified, update your README:

```markdown
**Live demo:** https://<your-domain>/docs
```

And update the CI deploy workflow (`.github/workflows/deploy.yml`):

```bash
railway login --browserless   # generates a token
# Copy the token → GitHub repo → Settings → Secrets → RAILWAY_TOKEN
```

---

## Troubleshooting

**Build fails:** check `railway logs` — most common cause is a missing env var that pydantic-settings requires at startup. Cross-check against `.env.example`.

**Health returns degraded:** one of the dependency checks is failing. Run `railway logs` and look for `ConnectionError` or `OperationalError`.

**Migrations not applied:** run `railway run alembic upgrade head` manually. The DB might have started after the app on first deploy.

**Workers not processing:** verify `KAFKA_BOOTSTRAP_SERVERS` is set on the worker services, not just the app service. Each Railway service has its own variable scope.
