.PHONY: check lint typecheck test fmt

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest -q

fmt:
	uv run ruff check --fix .
	uv run ruff format .
