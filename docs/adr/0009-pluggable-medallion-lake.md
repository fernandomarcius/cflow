# ADR 0009 - Medallion lake on a pluggable object-storage backend, Iceberg for silver

Status: accepted

## Decision

External data lands in a medallion lake: **landing** (raw, immutable) -> **bronze** (Parquet,
faithful to the origin) -> **silver** (typed 1:1 with the origin, versioned per run, published as an
Apache Iceberg table or as Parquet plus a pointer) -> **gold** (conformed products). The storage is a
**pluggable backend** (`cflow_core.ingestao.armazenamento`): `local` (a directory; the default, used by
tests and by air-gapped installs) and `gcs` (Google Cloud Storage JSON API through `httpx`, service
account credential in the vault). No code writes to the lake through a vendor SDK directly.

Datasets may be partitioned; readers resolve the current state through an index/pointer
(`_atual.json`), never through a glob. The grain of each table is part of the source contract and a
blocking guard refuses to publish a silver table that violates it.

## Consequences

- The control plane (metadata, contracts, watermarks, audit, vault) never leaves your infrastructure;
  the lake backend only receives bytes.
- Iceberg metadata lives in a PostgreSQL JDBC catalog and can be read by external engines.
- A development switch (`CFLOW_LAKE_SOMENTE_LOCAL`) refuses any non-local lake connection so that a
  dev environment can never publish to production buckets.
