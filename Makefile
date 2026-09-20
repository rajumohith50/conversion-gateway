.PHONY: install test lint typecheck fmt run down migrate api

install:
	uv sync

test:
	uv run pytest

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
