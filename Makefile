.PHONY: check lint typecheck test fmt dashboard dashboard-check

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

# The dashboard is Preact + Vite; dashboard/dist is committed so running the router never needs Node.
dashboard:
	cd dashboard && npm ci && npm run build

dashboard-check:
	cd dashboard && npm ci && npx tsc --noEmit && npm run build
	git diff --exit-code dashboard/dist
