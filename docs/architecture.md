# Architecture

This document describes how cFlow is put together. The reasoning behind each major choice is in
[`adr/`](adr/); vocabulary is in the [glossary](glossary.md); the metadata schema is in the
[data model](data-model.md).

## 1. What cFlow is

cFlow is a self-hosted data platform built around **pipelines expressed as graphs**. A specialist
assembles a directed graph of nodes (source, SQL, join, comparison, Python, output), sees the result
of every step on a sample while editing, publishes a version, and the platform runs it on a schedule
or on demand. Every run keeps a full snapshot of every node, so any historical run can be inspected
step by step.

The design goal is to keep the "build a transformation step by step and look at each result" mental
model of spreadsheet/query tools while adding what they lack: scale, a credential vault, versioning,
an audit trail and forensic debugging.

## 2. Components

```
        web UI (Studio / Portal)                     other clients (CLI, services)
                  |                                            |
                  +--------------------+-----------------------+
                                       v
                            REST API  (cflow_api, FastAPI)
                                       |
              +------------------------+------------------------+
              v                        v                        v
       PostgreSQL (metadata,    Temporal server           lake (local dir | GCS)
       audit, vault, runs)      (orchestration)           landing / bronze / silver / gold
              ^                        ^                        ^
              |                        v                        |
              +------------ worker (cflow_worker) --------------+
                           activities: nodes, ingestion, reports
                                       |
                                  DuckDB per run
                                       |
                    external sources (SQL Server, Postgres, REST, files)
```

| Package | Role |
|---|---|
| `cflow_core` | Library shared by API and worker: auth, authorisation, audit, connections, vault, pipeline definitions, execution engine, ingestion, modelling, reports. |
| `cflow_api` | FastAPI service. The only entry point for clients. |
| `cflow_worker` | Temporal worker: runs pipeline nodes, ingestion slices and report generation; hosts the schedulers. |
| `migrations/` | Alembic migrations (explicit SQL) for the metadata database. |

The API and the web UI never talk to Temporal or DuckDB directly, and Temporal vocabulary never
reaches users (ADR 0003).

## 3. Data flow

```
sources -> graph (transformations) -> cube (measures) -> views (pivot) -> pages -> stamped report
```

Each link is versioned and recorded, so a number can always be traced back to the node and run that
produced it.

## 4. Pipelines

A pipeline definition is a JSON document `{nos: [...], arestas: [...], parametros: [...]}`.

Node types:

- `fonte` - reads from a **connection** (database, REST API, file), from **another pipeline**
  (ADR 0008), from the **lake** or from a **published** dataset.
- `sql`, `guiada` (form-driven nodes that generate SQL), `comparacao` - DuckDB SQL over the outputs
  of predecessor nodes.
- `python` - procedural code in a sandboxed subprocess with CPU/memory/file-size limits.
- `saida` - output to a file, to the lake (bronze/silver/gold, optionally Iceberg).
- **Effect nodes** that write outside the platform and are therefore never reused between runs:
  `escrita_sql`, `http`, `email`, `ingestao`. Each carries a per-(run, node) guard so a retried
  activity does not repeat the effect.

Parameters (`:name`) are provided at trigger time, always bound natively; "blank = all" unless the
parameter is mandatory; parameters flagged `segredo: true` are redacted everywhere they persist.

### Two ways to see a node's output

- **Edit mode (preview)**: recomputes over a sample in a session DuckDB; seconds of feedback; no
  Temporal; never publishes or triggers side effects.
- **Execution mode**: each node persists a Parquet snapshot plus metadata (rows, content hash,
  fingerprint, duration). Unchanged nodes are reused from a previous run by fingerprint.

## 5. Execution

Publishing a pipeline makes it runnable. A run is a Temporal workflow where each node is an activity
running DuckDB on a worker. Native benefits: per-node retry, immutable history, re-run from a given
node reusing earlier snapshots.

- **Runs and chains**: triggering a pipeline with upstream dependencies produces a *chain* of runs
  sharing a `cadeia_id`; the requested one is the root.
- **Re-execution** creates a *new* run linked to the original (`reexecucao_de`), optionally from a
  node; the original is never reopened.
- **Concurrency**: the same pipeline with the same parameters cannot be running twice; the guarantee
  is a partial unique index on the run table, not a pre-check.
- **Scheduler**: cron-style schedules (with optional chaining) fire runs from the worker.
- **Reconciliation**: runs left in `executando` by a dead workflow are closed by a periodic job that
  only acts when Temporal positively answered "unknown workflow".
- **Inline mode**: with `CFLOW_USAR_TEMPORAL=false` the same engine runs synchronously (tests, small
  setups).

## 6. Lake and ingestion

External public data does not enter through the graph; it enters through a scheduled **ingestion**
subsystem into a medallion lake (ADR 0009, ADR 0010):

```
source contract -> discover(window) -> [slice, ...] -> landing (raw, immutable) -> bronze (Parquet)
                                            |                   |
                                       304 = no-op        quarantine
```

Silver is the typed bronze, built by a regular pipeline and published as an Iceberg table (JDBC
catalog in PostgreSQL) or Parquet plus pointer. Gold holds conformed products. Datasets can be
partitioned by parameter or column and are read through their index, never by glob. A `dev`/`prd`
environment namespace can prefix the root of the gold layer so that test runs never move the pointer
production readers use.

## 7. Modelling and consumption

- **Cube**: the output of a pipeline published as dimensions and named measures. A formula is a
  governed measure with a name, owner and version, not a free cell.
- **View**: a saved pivot configuration over a cube, with the filters the operator may change.
- **Page**: a composition of blocks (view, chart, KPI, text) that only *references* the model; a page
  computes nothing.
- **Stamped report**: an export that carries a stamp (time, source run, page version, filters, user),
  closing the audit chain `report -> page -> view -> measure -> cube -> run -> node`.
- **Report layouts**: declarative layouts that turn a node output into an `.xlsx` artifact, generated
  asynchronously (own task queue) and stored immutably.

## 8. Security

- **RBAC** with four profiles (admin, specialist, operator, auditor) plus a least-privilege `servico`
  profile for service accounts that authenticate with API tokens and can only run and follow the
  pipelines they were granted. Access is per group and per resource (view / execute / edit /
  administer), with segregation of duties.
- **Authentication**: local password (Argon2) with brute-force lockout, optional TOTP MFA, OIDC SSO,
  server-side sessions (token hash only), password recovery.
- **Vault**: credentials are encrypted (Fernet) in a table separate from connection metadata; a secret
  goes in over the request body, is never returned, and is resolved only inside the worker.
- **Audit**: append-only, written in the same transaction as the change (ADR 0004).
- **Logs**: structured JSON with request-id correlation, separate from the permanent audit trail.

## 9. Deployment shapes

- `docker-compose.yml` for local/self-hosted use: PostgreSQL, Temporal (with PostgreSQL persistence,
  `--profile temporal`), the API, the worker and the web UI.
- Any container platform: the API and worker images are stateless; state is in PostgreSQL, the lake
  and the shared snapshot/upload volumes. See [operations](operations.md).
