# cFlow API image. Build context = repository root.
#
#   docker build -t ghcr.io/fernandomarcius/cflow-api .
#
# What goes in:
#   - core/ and api/ (the code);
#   - scripts/: maintenance routines run inside the container (`docker exec ... uv run python
#     scripts/backup.py ...`);
#   - migrations/ + Alembic (the `migracao` group): the deploy applies the schema with THIS image
#     before starting the new one: `sh -c "cd migrations && uv run alembic upgrade head"`, with
#     CFLOW_OWNER_DATABASE_URL and CFLOW_APP_PASSWORD set.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core ./core
COPY api ./api
RUN uv sync --frozen --no-dev --group migracao

COPY scripts ./scripts
COPY migrations ./migrations

# At run time nothing is resolved: `uv run` (in CMD and in `docker exec ... uv run python ...`) uses
# the already-installed environment instead of syncing the `dev` group at container start.
ENV UV_NO_SYNC=1

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "cflow_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
