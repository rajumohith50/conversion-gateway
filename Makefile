.PHONY: install test lint typecheck fmt run down up seed logs migrate api worker uploader queue-init reconcile mock-api dlq

install:
	uv sync

# PUBSUB_EMULATOR_HOST makes the Pub/Sub adapter tests run against the
# emulator from `make run`; unset, they are skipped.
test:
	PUBSUB_EMULATOR_HOST=localhost:8085 uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# Infrastructure only (Postgres + Pub/Sub emulator): what local development
# and `make test` need.
run:
	docker compose up -d postgres pubsub

# The whole system in containers. Then `make seed`.
up:
	docker compose up --build -d --wait

# Post the demo mix from inside the compose network and report outcomes.
seed:
	docker compose run --rm --no-deps -e SEED_API_URL=http://api:8080 api gateway seed

logs:
	docker compose logs -f api worker uploader

down:
	docker compose down --volumes --remove-orphans

# Apply ledger migrations to DATABASE_URL (from .env or the environment).
migrate:
	uv run alembic upgrade head

# Serve the ingest API locally against the compose Postgres.
api:
	uv run uvicorn gateway.api.app:create_app_from_env --factory --host 0.0.0.0 --port 8080 --reload

# Create the Pub/Sub topic and subscription on the emulator. Rerun after
# `make down`; the emulator has no persistence.
queue-init:
	uv run gateway queue-init

worker:
	METRICS_PORT=9091 uv run gateway worker

reconcile:
	uv run gateway reconcile

dlq:
	uv run gateway dlq list

uploader:
	METRICS_PORT=9092 uv run gateway uploader

# The mock ad platform, on the port .env.example points ADS_API_BASE_URL at.
mock-api:
	uv run uvicorn mock_ads_api.app:create_app_from_env --factory --host 0.0.0.0 --port 8081
