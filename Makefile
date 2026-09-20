.PHONY: install test lint fmt typecheck

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy
