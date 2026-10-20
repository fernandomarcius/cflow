.PHONY: up down migrate test test-core lint format sync logs psql backup verifica-backup

# Load .env (if present) for the targets that run on the host.
ifneq (,$(wildcard .env))
include .env
export
endif

# Bring up the whole local environment (postgres + temporal + api + worker + web).
up:
	docker compose --profile temporal up -d --build
	@echo "API at http://localhost:8000/saude"

down:
	docker compose --profile temporal down

# Apply the migrations (Alembic) connecting as cflow_owner (host -> localhost).
migrate:
	cd migrations && uv run --project .. alembic upgrade head

# Resolve the workspace dependencies (core, api).
sync:
	uv sync --all-packages

# Full suite (unit + integration). Integration needs the compose Postgres up and .env configured;
# it starts the worker inside pytest, so ./worker must exist.
test:
	uv run pytest

# Unit tests of the core only (no database needed).
test-core:
	uv run pytest core/tests

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

# Metadata backup (pg_dump custom format).
backup:
	uv run python scripts/backup.py dump backups/cflow.dump

# A backup that was never restored is not a backup: dump -> restore -> compare.
verifica-backup:
	uv run python scripts/backup.py verificar

logs:
	docker compose logs -f

psql:
	docker compose exec postgres psql -U $${POSTGRES_USER:-cflow_owner} -d $${POSTGRES_DB:-cflow}
