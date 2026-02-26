include .env
export

.PHONY: up down check-infra

up:
	docker compose up -d

down:
	docker compose down

check-infra:
	docker compose exec redis redis-cli ping
	docker compose exec postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -c "SELECT 1"
	docker compose exec kafka kafka-topics --list --bootstrap-server localhost:9092
