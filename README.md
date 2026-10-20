# cFlow

cFlow is a self-hosted data platform built around **pipelines expressed as graphs**. You assemble a
directed graph of nodes (sources, SQL, joins, comparisons, Python, outputs), see the result of every
step on a sample while you edit, publish a version, and let the platform run it on a schedule or on
demand. Execution is durable (Temporal), transformations run in DuckDB, results land in a medallion
lake (Parquet / Apache Iceberg), and everything that changes is recorded in an append-only audit
trail.

- **Graph pipelines** - SQL and guided nodes, sandboxed Python nodes, effect nodes (SQL write, HTTP,
  e-mail), pipelines that consume other pipelines, reusable DuckDB macro libraries.
- **Preview vs. execution** - instant sampled preview per node while editing; full Parquet snapshots
  per node in production runs, so any past run can be inspected step by step.
- **Durable execution** - each node is a Temporal activity: retries, history, re-run from a node,
  scheduling, chained runs, reconciliation of orphan runs.
- **Lake and ingestion** - declarative source contracts with connectors per access class, landing /
  bronze / silver / gold layers, Iceberg tables, partitioned datasets, quarantine and replay.
- **Governance** - RBAC with segregation of duties, credential vault, MFA, OIDC SSO, API tokens for
  service accounts, versioned definitions, stamped reports.
- **Self-hosted** - PostgreSQL + Temporal + a local directory or an object store. Nothing phones home.

## Repository layout

| Path | Contents |
|---|---|
| `core/` | `cflow-core` Python library (`cflow_core`): auth, audit, connections, vault, pipelines, engine, ingestion, modelling, reports |
| `api/` | `cflow-api` FastAPI service (`cflow_api`) |
| `worker/` | `cflow-worker` Temporal worker (`cflow_worker`) |
| `web/` | web UI (Studio and Portal) |
| `migrations/` | Alembic migrations of the metadata database |
| `scripts/` | maintenance scripts (backup, service accounts) |
| `docs/` | architecture, glossary, data model, operations, ADRs |
| `deploy/` | support files for the compose stack (Temporal dynamic config, Postgres init) |

## Quick start

Requirements: Docker with Compose, [uv](https://docs.astral.sh/uv/) for local development.

```bash
cp .env.example .env
# edit .env: set the passwords and generate a vault key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

make up         # postgres + temporal + api + worker + web
make migrate    # apply the migrations (creates roles, schema and the bootstrap admin)
```

- API: <http://localhost:8000/saude> (liveness), <http://localhost:8000/docs> (OpenAPI)
- Web UI: <http://localhost:5173>
- Temporal UI: <http://localhost:8233>

The API answers on `/` and also under the `/api/v1` prefix, so it works behind a reverse proxy that
keeps or strips the prefix.

## Development

```bash
uv sync --all-packages
make lint        # ruff
make test-core   # unit tests, no database
make test        # full suite (needs the compose Postgres and Temporal)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [documentation](docs/README.md).

## Images

`ghcr.io/fernandomarcius/cflow-api`, `ghcr.io/fernandomarcius/cflow-worker`,
`ghcr.io/fernandomarcius/cflow-web`.

## License

Apache License 2.0 - see [LICENSE](LICENSE).
