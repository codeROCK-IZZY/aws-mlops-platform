# Common developer commands. Run `make help`.
.PHONY: help up down demo logs test test-backend test-frontend lint lifecycle

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

up: ## Build and start the full stack (dashboard http://localhost:8088)
	docker compose up -d --build

down: ## Stop the stack (keeps data volumes)
	docker compose down

demo: ## Demo users (erin engineer, amir approver, vera viewer, admin) + a year of data through the pipeline
	docker compose run --rm api python -m app.cli demo

logs: ## Follow worker and model-server logs
	docker compose logs -f worker serving

test: test-backend test-frontend ## Run all tests

test-backend: ## Backend tests (needs PostgreSQL; see docs/DEVELOPMENT.md)
	cd backend && pytest

test-frontend: ## Frontend unit tests
	cd frontend && npm test

lint: ## Lint and type-check everything
	cd backend && ruff check app tests scripts migrations && ruff format --check app tests scripts migrations && mypy app scripts
	cd frontend && npm run lint && npm run typecheck

lifecycle: ## Replay the year without Docker services (moto S3, SQLite MLflow) and regenerate eval/reports/
	cd backend && python scripts/lifecycle_offline.py --out ../eval/reports --gate
