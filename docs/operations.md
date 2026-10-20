# Operations and maintenance

Practical notes for running cFlow. Every item states the failure mode it prevents.

## 1. Keep metadata transactions short

The metadata database is not the bottleneck; what hurts at high run cadence is what grows unattended
(disk, temporary files, expired sessions) and a transaction left open while an external source is
being read.

Rule enforced in the code base: resolving a credential from the vault happens in a **short
transaction that closes**; the conversation with the external source (SQL Server, REST API, file)
happens afterwards, outside any transaction. `conexoes.obter_acesso(connection_id)` returns an
`Acesso` (type, config, secret) and the helpers `amostra`, `testar`, `navegar_schema` and `colunas`
do not receive a database connection at all, so the problem cannot be reintroduced by accident.

Why it matters: an open transaction pins `xmin` for the whole database, so autovacuum cannot
reclaim dead tuples of *any* table, and every overlapping run holds a pool connection. The compose
file also sets `idle_in_transaction_session_timeout=15min` as a safety net (it kills an open *idle*
transaction, never a running query). Regression test:
`api/tests/integration/test_conexoes_transacao_curta.py` samples `pg_stat_activity` at the instant
the source is read and fails if a new `idle in transaction` backend appears.

## 2. Disk: what grows and who reclaims it

| What | Reclaimed by |
|---|---|
| Run snapshots (`CFLOW_SNAPSHOT_DIR`) | retention job, `CFLOW_SNAPSHOT_RETENCAO_DIAS` (default 30), keeps the latest run of each pipeline |
| **Orphan** snapshots (the pipeline was deleted, the Parquet stayed) | `execucao.manutencao.expurgar_snapshots_orfaos` |
| Temporary JSON/Parquet files in `/tmp` of the worker | removed in `finally` blocks at the source; the maintenance job is a safety net |
| Expired sessions and old login attempts | `cflow_core.auth.expurgo` |
| Container build cache | `docker builder prune` on a schedule (host concern) |

`expurgar_snapshots_orfaos` decides what to delete by comparing the disk with the run table. Pointed
at the database of *another* environment it would classify everything as orphan, so it has three
guards: a database with no runs at all deletes nothing; more than 95% of the directory looking
orphan is refused unless `forcar=True`; a directory created less than a day ago is never touched.
Tests isolate `snapshot_dir` in a temporary directory.

"Snapshot" means two unrelated things: the Parquet of a node in a pipeline run (this section) and the
version of an Iceberg table (a `overwrite` emits two per publication). They have different storage
and retention.

The audit trail is never part of any purge.

## 3. Backup

`scripts/backup.py` wraps `pg_dump -Fc` / `pg_restore` of the metadata database (the audit trail is
included):

```bash
python scripts/backup.py dump backups/cflow.dump      # write a dump
python scripts/backup.py verificar                    # dump -> restore into a temp DB -> compare counts -> drop
```

A backup that was never restored is not a backup: schedule `verificar` too (weekly is enough) and
keep the dump retention (`CFLOW_BACKUP_KEEP`, default 7) outside any directory a cleanup job sweeps.
The lake and the snapshot volume are backed up with the tooling of their storage.

## 4. Stuck runs

A run leaves `executando` only when the workflow itself writes the outcome. A workflow that dies
first (orchestrator restarted, workflow terminated, history expired) would leave the row
`executando` forever. The worker therefore runs a reconciliation every few minutes under an advisory
lock (only one replica queries the orchestrator): it asks Temporal and closes what Temporal does not
know any more, auditing it as a `sistema` action. It only marks a failure when Temporal *answered*;
if the orchestrator is down every run looks orphan, and in that case the job touches nothing and
says why. Triggering the same pipeline again also closes an orphan on the spot.

## 5. Temporal persistence

The compose file runs `temporalio/server` with persistence in the same PostgreSQL, in dedicated
databases and a dedicated role (`temporal_app`; `temporal` and `temporal_visibility`). A dev server
without a volume loses history on every restart, which is exactly how runs get stuck. The role is
created automatically the first time the PostgreSQL volume is initialised
(`deploy/postgres/init-temporal-role.sh`, using `TEMPORAL_DB_USER` / `TEMPORAL_DB_PASSWORD`); on an
existing volume create it by hand with `CREATE ROLE ... LOGIN CREATEDB`. Then start the stack with
`docker compose --profile temporal up -d`.

Acceptance check: restart the Temporal container in the middle of a run; the run must resume and
finish. History retention is `TEMPORAL_RETENCAO` (default 720h); watch PostgreSQL size if you raise it.

## 6. Database maintenance built into the migrations

- No exact duplicate indexes (a UNIQUE constraint plus a plain index over the same columns).
- Indexes on the foreign keys of tables that grow with use (sessions, runs, versions, reports).
  Foreign keys of small, hand-curated catalogues are intentionally left out.
- `auditoria(ocorrido_em, id)` matches the audit query (period filter, ordered by time).
- Per-table autovacuum for small hot tables (`agendamentos`, `pipelines`, ...) and `fillfactor=70`
  on `sessoes` and `agendamentos`, where the same row is rewritten constantly.
- `pg_stat_statements` is enabled by the compose file so slow-query investigation has data.

## 7. Health checks

- `GET /saude` (also `/healthz`): liveness; the API is up and PostgreSQL answers. The response
  shape is a contract: fields may be added, none renamed or removed.
- `GET /saude/pronto`: readiness; database, lake and Temporal. It does network I/O, so the lake
  verdict is cached for a short TTL; liveness never does network I/O beyond the database.
- The admin metrics endpoint (`/admin/...`) exposes counters (runs, ingestion freshness) for
  dashboards.

## 8. Configuration reference

All settings are environment variables with the `CFLOW_` prefix, defined in
`core/src/cflow_core/config.py` and listed with defaults in [`.env.example`](../.env.example).
Notable groups:

- **Database**: `CFLOW_DATABASE_URL` (runtime role), `CFLOW_OWNER_DATABASE_URL` (migrations),
  `CFLOW_DATABASE_TLS`, `CFLOW_DATABASE_CA_FILE`, `CFLOW_DATABASE_SCHEMA`, `CFLOW_DB_POOL_SIZE`,
  `CFLOW_DB_MAX_OVERFLOW`, `CFLOW_DB_ATRAS_DE_PGBOUNCER`.
- **Secrets as files**: any `<dir>/<name>/value` under `CFLOW_SECRETS_DIR` (default
  `/run/secrets/cflow`) becomes the variable `NAME` (upper-case, dashes to underscores) unless it is
  already set.
- **Auth**: session lifetimes, brute-force limits, password minimum, OIDC (`CFLOW_OIDC_*`), SMTP.
- **Vault**: `CFLOW_VAULT_KEY` (Fernet key).
- **Execution**: Temporal host/queues, worker concurrency, DuckDB memory/threads/spill directory,
  sandbox limits, source connection pool, HTTP node limits.
- **Lake and ingestion**: `CFLOW_LAKE_DIR`, `CFLOW_LAKE_SOMENTE_LOCAL`, retry/backoff and checksum
  options, download limits, PII tokenisation key.

Size the worker by container limits: DuckDB memory and threads default to the cgroup limits divided
by `CFLOW_WORKER_MAX_ATIVIDADES`; without a cgroup limit DuckDB would take most of the host RAM *per
connection*. Behind a pooler in transaction mode set `CFLOW_DB_ATRAS_DE_PGBOUNCER=true`.

## 9. Upgrading

1. Take a backup (`scripts/backup.py dump`).
2. Build the new API image and apply the migrations with it, as the owner role:
   `cd migrations && uv run alembic upgrade head` (needs `CFLOW_OWNER_DATABASE_URL` and
   `CFLOW_APP_PASSWORD`).
3. Roll the API, then the worker. Do not restart a worker while runs are in flight if you can avoid
   it; reconciliation will close what the restart orphans, but the run is lost.
