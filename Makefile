.PHONY: install test lint typecheck fmt run down

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
