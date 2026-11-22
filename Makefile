# =============================================================================
# Project #3: Idempotent Payment Processing System
# Makefile — every common operation has a named target
# Usage: make <target>
# =============================================================================

.PHONY: help setup dev down logs logs-app logs-worker shell test test-unit \
        test-integration test-cov migrate migrate-down migrate-status \
        check-infra load-test load-test-ui lint lint-fix typecheck fmt \
        clean clean-cache verify verify-quick verify-faults verify-faults-down bench recon demo dlq \
        tenant api-key load-test-redis-kill

# Default target — print available commands
help:
	@echo ""
	@echo "  Project #3: Idempotent Payment Processing System"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""

# -----------------------------------------------------------------------------
# Local development
# -----------------------------------------------------------------------------

# Host-side test settings: .env.test when it exists (make setup writes it).
# CI sets the same variables in the workflow and has no .env.test.
TEST_ENV = $(if $(wildcard .env.test),set -a; . ./.env.test; set +a;,)
PG_USER = $(or $(shell sed -n 's/^POSTGRES_USER=//p' .env 2>/dev/null),postgres)

setup: ## Fresh clone -> ready: env files, dependencies, Postgres + Redis, test database (then: make verify-quick)
	@command -v docker >/dev/null || (echo "setup needs Docker"; exit 1)
	@command -v poetry >/dev/null || (echo "setup needs Poetry (pipx install poetry)"; exit 1)
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	@test -f .env.test || (cp .env.test.example .env.test && echo "created .env.test from .env.test.example")
	poetry install --no-interaction
	docker compose up -d --wait postgres redis
	@docker compose exec -T postgres psql -U $(PG_USER) -d postgres -tAc \
		"SELECT 1 FROM pg_database WHERE datname = 'payments_test'" | grep -q 1 \
		|| docker compose exec -T postgres createdb -U $(PG_USER) payments_test
	@echo ""
	@echo "  Ready. Next: make verify-quick   (or make test, make dev, make demo)"
	@echo ""

dev: ## Start full local stack (app + Redis + Postgres + Kafka + Prometheus + Grafana)
	docker compose up -d --build
	@echo ""
	@echo "  API:        http://localhost:8000"
	@echo "  Docs:       http://localhost:8000/docs"
	@echo "  Metrics:    http://localhost:8000/metrics"
	@echo "  Prometheus: http://localhost:9090"
	@echo "  Grafana:    http://localhost:3000  (admin / admin)"
	@echo ""

down: ## Stop and remove all containers (keeps volumes)
	docker compose down

logs: ## Tail logs from all services
	docker compose logs -f

logs-app: ## Tail API logs only
	docker compose logs -f api

logs-worker: ## Tail outbox relay, charger, sweeper and webhook processor logs
	docker compose logs -f outbox_poller charger sweeper webhook_processor

shell: ## Open Python shell inside the running API container
	docker compose exec api .venv/bin/python

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

migrate: ## Apply all pending Alembic migrations
	docker compose exec api .venv/bin/alembic upgrade head

migrate-down: ## Roll back the last migration
	docker compose exec api .venv/bin/alembic downgrade -1

migrate-status: ## Show current migration version
	docker compose exec api .venv/bin/alembic current

# -----------------------------------------------------------------------------
# Infrastructure checks
# -----------------------------------------------------------------------------

check-infra: ## Verify all infrastructure services are healthy
	@echo "Checking Redis..."
	@docker compose exec redis redis-cli ping
	@echo "Checking PostgreSQL..."
	@docker compose exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "SELECT 1" -q -t'
	@echo "Checking Kafka..."
	@docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server localhost:9092
	@echo "Checking Prometheus..."
	@curl -sf http://localhost:9090/-/healthy && echo "Prometheus OK"
	@echo ""
	@echo "All infrastructure healthy."

# -----------------------------------------------------------------------------
# Testing
# -----------------------------------------------------------------------------

test: ## Run full suite (unit + integration) as CI does; needs Postgres + Redis (make setup)
	$(TEST_ENV) poetry run pytest tests/ -v --tb=short \
		--cov=app --cov-report=term-missing --cov-fail-under=80

test-unit: ## Run unit tests only — fast, no Docker services needed
	poetry run pytest tests/unit -v --tb=short

test-integration: ## Run integration tests against running Docker stack
	$(TEST_ENV) poetry run pytest tests/integration -v --tb=short

test-cov: ## Run tests with HTML coverage report (opens in browser)
	poetry run pytest tests/unit tests/integration \
		--cov=app --cov-report=html:htmlcov --cov-fail-under=80
	open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html

# -----------------------------------------------------------------------------
# Load testing
# -----------------------------------------------------------------------------

load-test: ## Run Locust load test headless (50 users, 60s); needs IPPS_API_KEY
	@test -n "$$IPPS_API_KEY" || (echo "export IPPS_API_KEY first (make tenant NAME=loadtest)"; exit 1)
	poetry run locust \
		-f tests/load/locustfile.py \
		--host http://localhost:8000 \
		--users 50 \
		--spawn-rate 5 \
		--run-time 60s \
		--headless \
		--print-stats

load-test-redis-kill: ## Stage 1 gate: Locust with Redis stopped mid-run; needs IPPS_API_KEY
	tests/load/redis_kill.sh

load-test-ui: ## Start Locust with browser UI at http://localhost:8089
	poetry run locust \
		-f tests/load/locustfile.py \
		--host http://localhost:8000

# -----------------------------------------------------------------------------
# Code quality
# -----------------------------------------------------------------------------

lint: ## Run ruff linter
	poetry run ruff check .

lint-fix: ## Run ruff and auto-fix issues
	poetry run ruff check . --fix

typecheck: ## Run mypy type checker
	poetry run mypy .

fmt: ## Format code with ruff formatter
	poetry run ruff format .

# -----------------------------------------------------------------------------
# Tenants and API keys (keys are printed once; only their hash is stored)
# -----------------------------------------------------------------------------

tenant: ## Create a tenant and its first API key: make tenant NAME=acme
	@test -n "$(NAME)" || (echo "usage: make tenant NAME=<name>"; exit 1)
	docker compose exec api .venv/bin/python -m app.cli create-tenant --name "$(NAME)"

api-key: ## Add an API key to a tenant: make api-key TENANT=<uuid|default>
	@test -n "$(TENANT)" || (echo "usage: make api-key TENANT=<uuid|default>"; exit 1)
	docker compose exec api .venv/bin/python -m app.cli create-api-key --tenant-id "$(TENANT)"

# -----------------------------------------------------------------------------
# Verification harness (docs/verification.md). Needs Postgres + Redis, like
# `make test` (make setup). VERIFY_EXAMPLES overrides the number of histories.
# -----------------------------------------------------------------------------

verify-quick: ## State machine, short derandomized profile (~20 histories) + self-test, as PR CI
	$(TEST_ENV) VERIFY_PROFILE=pr LOG_LEVEL=ERROR poetry run pytest tests/verification -s --no-cov -p no:cacheprovider

verify: ## State machine, nightly profile (300 random histories) + self-test; histories in .verify/
	$(TEST_ENV) VERIFY_PROFILE=nightly VERIFY_HISTORY_DIR=.verify LOG_LEVEL=ERROR \
		poetry run pytest tests/verification -s --no-cov

# The fault layer runs the whole system in its own compose project
# (ipps-faults: own containers, ports and tmpfs database; the dev stack and
# its data are untouched). On success the stack is removed; on a violation it
# is left running for inspection (make verify-faults-down removes it).
# FAULT_SCENARIOS, FAULT_SECONDS and FAULT_SEEDS=7,12 override the run.
FAULTS_COMPOSE = docker compose -f docker-compose.faults.yml
FAULTS_ENV = APP_ENV=test LOG_LEVEL=ERROR \
	DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/payments_faults \
	REDIS_URL=redis://localhost:56379/0 KAFKA_BOOTSTRAP_SERVERS=localhost:9092

verify-faults: ## Fault layer: seeded scenarios against the full stack behind Toxiproxy + self-test (~10 min)
	$(FAULTS_COMPOSE) up -d --build
	$(FAULTS_ENV) poetry run python -m verification.faults --selftest \
		&& $(FAULTS_COMPOSE) down -v

verify-faults-down: ## Remove the fault stack and its data
	$(FAULTS_COMPOSE) down -v

# Scripts that drive the running dev stack from the host. They reach Postgres
# with the owner credentials in .env; the variables below only satisfy the
# app's settings on import.
SCRIPT_ENV = APP_ENV=development LOG_LEVEL=WARNING \
	DATABASE_URL=postgresql+asyncpg://unused@localhost/unused \
	REDIS_URL=redis://localhost:6379/0 KAFKA_BOOTSTRAP_SERVERS=localhost:9092

bench: ## Latency + outbox lag + time to capture on the dev stack, with the hardware (make dev first; BENCH_USERS, BENCH_SECONDS)
	$(SCRIPT_ENV) poetry run python -m scripts.bench

dlq: ## Dead letters: payments/refunds given up on, dead webhooks, dlq.v1 events (docs/runbooks/dlq-growing.md)
	@docker compose exec -T postgres psql -U $(PG_USER) -d $(or $(shell sed -n 's/^POSTGRES_DB=//p' .env 2>/dev/null),payments_db) -c \
		"SELECT 'payments PROCESSING, retries exhausted' AS dead_letters, count(*) FROM payments WHERE status = 'PROCESSING' AND next_attempt_at IS NULL AND wallet_id IS NOT NULL \
		 UNION ALL SELECT 'refunds PROCESSING, retries exhausted', count(*) FROM refunds WHERE status = 'PROCESSING' AND next_attempt_at IS NULL \
		 UNION ALL SELECT 'webhook events dead', count(*) FROM webhook_events WHERE status = 'dead' \
		 UNION ALL SELECT 'dlq.v1 events, last 24 h', count(*) FROM outbox_events WHERE event_type = 'dlq.v1' AND created_at > now() - interval '24 hours'" \
		-c "SELECT provider_event_id, event_type, attempts, left(last_error, 100) AS last_error FROM webhook_events WHERE status = 'dead' ORDER BY received_at DESC LIMIT 5"

recon: ## Reconcile a provider file date (UTC): make recon [DATE=YYYY-MM-DD], default today
	docker compose exec api .venv/bin/python -m app.workers.recon_job --date "$(or $(DATE),$(shell date -u +%F))"

demo: ## The three-minute demo, every claim checked (make dev first; DEMO_PAUSE=2 for recording)
	$(SCRIPT_ENV) poetry run python -m scripts.demo

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------

clean: ## Stop containers and remove all volumes (wipes DB data)
	docker compose down -v
	@echo "All containers and volumes removed."

clean-cache: ## Remove Python bytecode and pytest cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
