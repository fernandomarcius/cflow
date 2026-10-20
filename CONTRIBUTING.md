# Contributing

Thanks for your interest in cFlow.

## Setup

```bash
cp .env.example .env            # adjust passwords and generate CFLOW_VAULT_KEY
make up                         # postgres + temporal + api + worker + web
make migrate                    # apply the migrations (as cflow_owner)
uv sync --all-packages          # local Python environment
```

## Checks before opening a pull request

```bash
make lint                       # ruff check + ruff format --check
make test-core                  # unit tests, no database needed
make test                       # full suite (needs the compose Postgres and ./worker)
```

## Guidelines

- Keep changes focused; add or update tests with the behaviour you change.
- Identifiers in the code base are mostly Portuguese (tables, functions, JSON keys, error codes);
  keep that style when extending existing modules. Comments, docstrings, messages and documentation
  are in English.
- Database changes go through a new Alembic migration written as explicit SQL (no autogenerate), with
  the matching grants for the `cflow_app` role. Never edit a published migration.
- Values always travel as bind parameters; only validated identifiers are interpolated into SQL.
- Secrets never appear in logs, audit diffs or API responses.
- Only permissively licensed dependencies (MIT/BSD/Apache-2.0).

By contributing you agree that your contribution is licensed under the Apache License 2.0.
