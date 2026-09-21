.PHONY: install test lint typecheck fmt run down migrate api worker uploader queue-init reconcile mock-api

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

# Bring up the local stack. Later phases add the gateway, worker and mock ads
# API services to docker-compose.yml; the target does not change.
run:
	docker compose up --build -d

down:
	docker compose down --volumes

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
	uv run gateway worker

reconcile:
	uv run gateway reconcile

uploader:
	uv run gateway uploader

# The mock ad platform, on the port .env.example points ADS_API_BASE_URL at.
mock-api:
	uv run uvicorn mock_ads_api.app:create_app_from_env --factory --host 0.0.0.0 --port 8081
