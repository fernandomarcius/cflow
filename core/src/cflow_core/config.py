"""Environment-based configuration. No secret lives in code: everything comes from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .runtime import exportar_segredos_arquivo


class Settings(BaseSettings):
    """Application settings. Read from the environment (and from a .env file in dev)."""

    model_config = SettingsConfigDict(env_prefix="CFLOW_", env_file=".env", extra="ignore")

    # Runtime URL: the application connects ONLY as cflow_app (never as the owner).
    database_url: str = Field(
        default="postgresql+asyncpg://cflow_app:change-me@localhost:5432/cflow",
        description="asyncpg DSN of the cflow_app runtime role.",
    )
    database_tls: bool = Field(
        default=False,
        description="Connect to Postgres over TLS (CFLOW_DATABASE_TLS).",
    )
    database_ca_file: str | None = Field(
        default=None,
        description="CA certificate of the Postgres server (verify-ca) (CFLOW_DATABASE_CA_FILE).",
    )
    database_schema: str | None = Field(
        default=None,
        description="search_path of the connections (CFLOW_DATABASE_SCHEMA). Empty: server default.",
    )
    tz: str = Field(default="America/Sao_Paulo", description="Display time zone at the edge.")
    log_level: str = Field(default="INFO")
    temporal_host: str = Field(default="localhost:7233")
    temporal_task_queue: str = Field(
        default="cflow-execucao",
        description="Task queue of the execution workflow. Tests use an isolated queue (no clash with a dev worker).",
    )
    usar_temporal: bool = Field(
        default=True,
        description="Default: execution orchestrated by Temporal (worker); each node = one activity. false: inline (synchronous, no worker).",
    )

    # --- Authentication ---
    # A 30-minute inactivity window logged out people who were actively WORKING: the Studio edits
    # SQL, drags nodes and writes templates on the client without touching the API, so half an
    # hour of real work counted as "inactivity". The window now covers a working day; the
    # absolute expiry still closes the day.
    sessao_inatividade_segundos: int = Field(
        default=28800, description="Inactivity logout (8 h, a working day)."
    )
    sessao_absoluta_segundos: int = Field(
        default=86400, description="Absolute session expiry (24 h)."
    )
    recuperacao_expira_segundos: int = Field(
        default=1800, description="Validity of the password recovery token."
    )
    primeira_senha_expira_segundos: int = Field(
        default=259200, description="Validity of the first-password token (72 h)."
    )
    forca_bruta_max: int = Field(default=5, description="Failures before lockout.")
    forca_bruta_janela_segundos: int = Field(default=900, description="Lockout window (15 min).")
    senha_min: int = Field(default=12, description="Minimum password length (NIST).")
    # Admin bootstrap (read by migration 0003). No default password.
    admin_email: str | None = Field(default=None)
    admin_password: str | None = Field(default=None)

    # Notification channel (recovery). Without SMTP configured: console fallback (air-gapped).
    # `smtp_tls` (STARTTLS) and `smtp_user`/`smtp_password` enable authenticated relays (e.g. 587).
    smtp_host: str | None = Field(default=None)
    smtp_porta: int = Field(default=25)
    smtp_remetente: str | None = Field(default=None)
    smtp_tls: bool = Field(
        default=False, description="STARTTLS before authenticating/sending (587)."
    )
    smtp_user: str | None = Field(default=None)
    smtp_password: str | None = Field(default=None)
    # Public URL of the front end (no trailing slash), used to build the set-password link in the
    # e-mail (e.g. https://cflow.example.com). Without it, the e-mail delivers only the code (token).
    app_url: str | None = Field(default=None)

    # Credential vault: Fernet key (urlsafe base64, 32 bytes). No default (security).
    vault_key: str | None = Field(default=None)
    conexao_teste_timeout_segundos: int = Field(default=5)
    # Timeout of the sample/preview QUERY: a real query (join/aggregation) on the source, longer
    # than the connection test (5s) but much shorter than the full execution.
    conexao_amostra_timeout_segundos: int = Field(default=30)
    # Timeout of the execution QUERY (full data): large operations take longer than the short test.
    conexao_query_timeout_segundos: int = Field(default=600)
    # SQL-over-REST gateway (api_query): protects against bursts (e.g. fan-out of N operations of
    # a schedule). Limits concurrent gateway calls, reuses the token (avoids a re-login per call
    # -> 429) and retries with backoff on 429.
    gateway_max_concorrencia: int = Field(
        default=4, description="Max simultaneous calls to the gateway."
    )
    gateway_token_ttl_segundos: int = Field(
        default=240, description="Reuse window of the gateway Bearer token."
    )
    gateway_max_tentativas: int = Field(
        default=4, description="Attempts on 429 (exponential backoff)."
    )
    # Source connection pool in the worker (OPT-IN). Reuses live database connections across
    # fan-out executions (cuts the re-login cost). Only EXECUTION reads use the pool; preview/test
    # keep a fresh connection. The secret stays on the connection for at most the idle TTL (worker
    # only). See conexoes/pool.py.
    pool_fontes: bool = Field(
        default=True,
        description="Enables the source connection pool in the worker (CFLOW_POOL_FONTES).",
    )
    pool_fontes_ttl_segundos: int = Field(
        default=60,
        description="Idle TTL of a source connection in the pool (recycle/health-check).",
    )
    pool_fontes_max: int = Field(
        default=4, description="Max idle connections per destination in the pool."
    )

    # Studio preview session (cache of the source sample).
    preview_sessao_ttl_segundos: int = Field(
        default=900, description="Expiry on inactivity (15 min)."
    )
    preview_sessao_max: int = Field(
        default=50, description="Ceiling of simultaneous preview sessions (LRU)."
    )

    # --- Scalable worker ---
    worker_max_atividades: int = Field(
        default=8,
        description="Simultaneous activities per worker process (= executor threads).",
    )
    worker_vagas_atividades: int | None = Field(
        default=None,
        description=(
            "Activity slots of the execution queue (`max_concurrent_activities`). Unset: equal to "
            "CFLOW_WORKER_MAX_ATIVIDADES. Above it, the surplus is for nodes that wait on the "
            "network (object-storage publishing); CPU work stays bounded by the executor threads "
            "and by the DuckDB instance ceiling, which follow CFLOW_WORKER_MAX_ATIVIDADES."
        ),
    )
    extracao_concorrencia_max: int | None = Field(
        default=None,
        description=(
            "Ceiling of simultaneous full SQL Server extractions per process. Unset: "
            "CFLOW_WORKER_MAX_ATIVIDADES (the previous implicit limit). Measured over a VPN link: "
            "with 3-4 large extractions at once the stream stalls a few minutes after it starts "
            "and queries die on the 600 s timeout, while a single extraction ran to completion."
        ),
    )
    extracao_em_processo: bool = Field(
        default=False,
        description=(
            "Full SQL Server extraction in a pool process, without slicing (same query, decode "
            "outside the worker's GIL). Applies to every execution source; the process ceiling is "
            "CFLOW_EXTRACAO_PROCESSOS_MAX, bounded by the container CPUs."
        ),
    )
    extracao_processos_max: int = Field(
        default=4,
        description="Process ceiling per sliced extraction (node with config.extracao_paralela). Also bounded by the visible CPUs.",
    )
    worker_ticks: bool = Field(
        default=True,
        description="Runs the periodic loops (scheduler, ingestion, reaper) in this process. false in an activities-only Deployment.",
    )
    duckdb_memory_limit: str | None = Field(
        default=None,
        description="DuckDB `memory_limit` per connection (e.g. 2GB). Unset: derived from the cgroup.",
    )
    duckdb_threads: int | None = Field(
        default=None, description="DuckDB `threads` per connection. Unset: derived from the cgroup."
    )
    duckdb_compartilhado: bool = Field(
        default=False,
        description=(
            "One DuckDB instance per PROCESS (schema per node) instead of one per node. Single "
            "budget (the container's) and a single thread pool; in exchange, a greedy node "
            "affects its neighbour."
        ),
    )
    duckdb_temp_dir: str | None = Field(
        default=None,
        description="DuckDB `temp_directory` (spill). On Kubernetes, an emptyDir with a sizeLimit.",
    )
    lake_io_threads: int | None = Field(
        default=None,
        description=(
            "Threads of the executor DEDICATED to lake I/O (see cflow_core/io_lake.py). Unset: "
            "twice CFLOW_WORKER_MAX_ATIVIDADES, floor 4. They block on disk/network and do not "
            "compete for CPU with DuckDB, so they do not share the activities pool."
        ),
    )
    db_pool_size: int = Field(
        default=20,
        description=(
            "SQLAlchemy pool_size per event loop. 20, not 5: the engine reaches 53 nodes in "
            "flight at once, and with 5 persistent connections each one opened and closed a new "
            "connection (handshake + auth per checkout). Adds up to 30 connections per PROCESS "
            "with the overflow below: sum the processes that talk to the same Postgres before "
            "raising it."
        ),
    )
    db_max_overflow: int = Field(
        default=10,
        description=(
            "SQLAlchemy max_overflow per event loop. Kept at 10: measured, 20+10 matches 20+20; "
            "past the persistent pool, an ephemeral connection costs more than waiting for one."
        ),
    )
    db_atras_de_pgbouncer: bool = Field(
        default=False,
        description="Connection goes through pgbouncer in transaction mode: disables the asyncpg prepared statement cache.",
    )
    metricas_porta: int | None = Field(
        default=None,
        description="Prometheus port of the Temporal SDK in the worker (e.g. 9464). Unset: disabled.",
    )

    # Execution. Per-node Parquet snapshots on disk (self-hosted).
    snapshot_dir: str = Field(
        default="snapshots", description="Root directory of the Parquet snapshots."
    )

    # Publishing (output node). Stable, named destination, readable by external services.
    export_dir: str = Field(
        default="export",
        description="Root directory of the datasets published by the output node (external consumption).",
    )

    # External ingestion: lake root when the backend is `local`, the air-gapped default. With an
    # `objeto` connection with provedor=gcs, the destination becomes the bucket.
    lake_dir: str = Field(
        default="lake",
        description="Root of the medallion lake on the local backend (landing/bronze/quarantine layers).",
    )
    # OPT-IN guard for development and CI: the process only talks to the LOCAL lake. Re-pointing
    # the lake connection at the production buckets made a dev run publish there and move the
    # pointer the consumer reads, and "in dev the lake is local" was only a convention nobody
    # checked. When on, mounting non-local storage is refused with `lake_somente_local` before
    # resolving any credential or touching the network (`ingestao.armazenamento`). Off by default
    # on purpose: production does not define the variable.
    lake_somente_local: bool = Field(
        default=False,
        description="Refuses to open the lake on any provider other than the local one (dev, CI, tests). "
        "Production does not set the variable.",
    )
    # PII tokenization on write: the full name of the HMAC key VERSION in Cloud KMS,
    # `projects/<p>/locations/<l>/keyRings/<kr>/cryptoKeys/<k>/cryptoKeyVersions/<n>`.
    #
    # It is the VERSION, not the key: `macSign` signs per version, and pointing at the key returns
    # a 404 HTML page from the Google frontend, which does not even look like an API error. Pinning
    # the version is also the right design, because rotating changes the token of every person:
    # the switch must be deliberate and followed by a republish, never inherited from a version
    # that appeared on its own.
    #
    # Empty = no tokenizer, and then publishing a dataset with a classified column is REFUSED
    # (`pii_sem_tokenizador`) instead of writing the identifier in clear text; see
    # `ingestao/tokenizacao.py`.
    #
    # Not a secret: it is the resource NAME. The signer is the lake service account, through the
    # token the backend already gets from the vault; without `roles/cloudkms.signerVerifier` KMS
    # refuses even with the right name.
    pii_chave_kms: str = Field(
        default="",
        description="Full name of the KMS HMAC key that tokenizes personal data on write. "
        "Empty disables tokenization, and publishing a dataset with PII is then refused.",
    )
    ingestao_task_queue: str = Field(
        default="cflow-ingestao",
        description="Dedicated task queue of ingestion, isolated from the pipeline queue so that "
        "a ten-year backfill does not eat the slots of report execution.",
    )
    relatorios_task_queue: str = Field(
        default="cflow-relatorios",
        description="Dedicated task queue of report generation (xlsx). A table with millions of "
        "rows takes minutes to become a spreadsheet and must not delay other work.",
    )
    relatorios_concorrencia: int = Field(
        default=4,
        description="How many report files the worker generates at once (one file = one "
        "activity). In series, the ~41 artifacts of a large operation would take ~20 min.",
    )
    lake_cache_dir: str = Field(
        default="lake_cache",
        description="Local cache of the lake objects read by pipelines. It is DERIVED and can be "
        "deleted at any time: the lake path is content-addressed and the layer is immutable, so "
        "the cache never goes stale. Not used on the local backend (reads the object directly).",
    )
    lake_preview_fatias: int = Field(
        default=1,
        description="How many slices (the most recent) the lake-source node preview materializes. "
        "Sampling the whole dataset would download 32 files to show 10 rows.",
    )
    ingestao_http_timeout_segundos: int = Field(
        default=180, description="Default timeout per download request to an external source."
    )
    ingestao_download_max_mb: int = Field(
        default=512,
        description="Byte ceiling of a single download (memory guard: the payload is read "
        "whole to hash before writing).",
    )

    # Durability of writes to the remote lake. Retry PER REQUEST, which composes with Temporal's
    # without duplicating it: Temporal's retries the whole SLICE and redoes the download from the
    # public origin (a 503 on a single PUT would cost re-downloading the file, which is how one
    # gets IP-blocked); this one costs nothing outside the cloud. Deliberately short budget: 4
    # attempts with a 20 s ceiling give ~35 s wall time in the worst case, inside the activity's
    # 30 min start_to_close.
    lake_max_tentativas: int = Field(
        default=4,
        description="Attempts per request to the lake object storage (1 = no "
        "retry). Only 408/429/5xx and transport failures retry; 412 never.",
    )
    lake_backoff_teto_segundos: int = Field(
        default=20,
        description="Ceiling of a wait between lake attempts (the server's Retry-After "
        "beats the exponential, but is still subject to this ceiling).",
    )
    lake_verificar_checksum: bool = Field(
        default=True,
        description="Sends the md5 on upload (x-goog-hash) and checks what GCS returns; on "
        "read, checks the bytes against the response's x-goog-hash.",
    )
    lake_cache_max_mib: int | None = Field(
        default=None,
        ge=1,
        description="Ceiling, in MiB, of the local lake cache (`lake_cache_dir`). Unset = no ceiling. "
        "With it, each new download deletes the least recently used cache files until it fits: "
        "only those idle for at least 10 min, never the one just downloaded. On "
        "Kubernetes, an exhausted ephemeral volume evicts the whole POD, with the runs in flight.",
    )
    lake_verificar_cache: bool = Field(
        default=False,
        description="Re-downloads and re-checks the object even with the local cache present. Off "
        "by default: the path is content-addressed and the layer immutable, so checking on every "
        "read would cost a download per query, which is why the cache exists. Turn on "
        "to investigate suspected corruption.",
    )

    # Upload of source files (Excel/CSV/Parquet). Volume shared between api and worker.
    upload_dir: str = Field(
        default="/app/uploads",
        description="Root directory of uploaded files (consumed by the 'arquivo' connector).",
    )

    # `python` node (sandbox): isolated (subprocess) execution of a procedural script, for what
    # SQL does not cover. Deterministic by contract (pure function of its inputs).
    sandbox_timeout_segundos: int = Field(
        default=600, description="Maximum time of a python node (subprocess timeout)."
    )
    sandbox_memoria_mb: int = Field(
        default=2048, description="Memory limit (RLIMIT_AS) of the python node; 0 = unlimited."
    )
    sandbox_saida_max_mb: int = Field(
        default=4096,
        description="Maximum file size the python node may write (RLIMIT_FSIZE).",
    )
    # EFFECT nodes. `http`: default per-request timeout and effective concurrency ceiling (the
    # node asks for `concorrencia` but never exceeds this ceiling). Writes use vault/connectors.
    http_no_timeout_segundos: int = Field(
        default=30, description="Default timeout (s) per request of the http node."
    )
    http_no_concorrencia_max: int = Field(
        default=16, description="Ceiling of simultaneous requests of an http node."
    )

    # SSO / OIDC. Without an issuer configured, the SSO endpoint is unavailable.
    oidc_issuer: str | None = Field(default=None)
    oidc_client_id: str | None = Field(default=None)
    oidc_jwks_uri: str | None = Field(
        default=None, description="If absent, derived from the issuer discovery."
    )
    oidc_perfil_padrao: str = Field(
        default="operador", description="Profile of users provisioned by SSO."
    )


@lru_cache
def get_settings() -> Settings:
    """Configuration singleton.

    Secrets delivered as files (see `cflow_core.runtime`) become variables BEFORE `Settings` reads
    the environment (`CFLOW_VAULT_KEY`, `CFLOW_SMTP_PASSWORD`, ...). Without the directory this
    does nothing."""
    exportar_segredos_arquivo()
    return Settings()
