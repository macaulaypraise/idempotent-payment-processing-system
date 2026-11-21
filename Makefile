# =============================================================================
# Project #3: Idempotent Payment Processing System
# Makefile — every common operation has a named target
# Usage: make <target>
# =============================================================================

.PHONY: help dev down logs logs-app logs-worker shell test test-unit \
        test-integration test-cov migrate migrate-down migrate-status \
        check-infra load-test load-test-ui lint lint-fix typecheck fmt \
        clean clean-cache verify verify-quick verify-faults bench recon demo \
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

test: ## Run full suite (unit + integration) as CI does; needs Postgres + Redis (see .env.test.example)
	poetry run pytest tests/ -v --tb=short \
		--cov=app --cov-report=term-missing --cov-fail-under=80

test-unit: ## Run unit tests only — fast, no Docker services needed
	poetry run pytest tests/unit -v --tb=short

test-integration: ## Run integration tests against running Docker stack
	poetry run pytest tests/integration -v --tb=short

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
# v2 placeholders — implemented in the stage noted. They fail on purpose so
# nothing can report a green "verify" before the harness exists.
# -----------------------------------------------------------------------------

verify: ## [Stage 7] Full verification run: state machine + fault scenarios + checker
	@echo "make verify: not implemented yet (Stage 7)"; exit 1

verify-quick: ## [Stage 7] Short deterministic verification profile (PR CI)
	@echo "make verify-quick: not implemented yet (Stage 7)"; exit 1

verify-faults: ## [Stage 7] Toxiproxy fault-injection scenarios
	@echo "make verify-faults: not implemented yet (Stage 7)"; exit 1

bench: ## [Stage 8] Headless Locust benchmark with CSV output
	@echo "make bench: not implemented yet (Stage 8)"; exit 1

recon: ## Reconcile a provider file date (UTC): make recon [DATE=YYYY-MM-DD], default today
	docker compose exec api .venv/bin/python -m app.workers.recon_job --date "$(or $(DATE),$(shell date -u +%F))"

demo: ## [Stage 8] Scripted three-minute demo
	@echo "make demo: not implemented yet (Stage 8)"; exit 1

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
