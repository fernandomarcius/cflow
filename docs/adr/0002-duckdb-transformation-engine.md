# ADR 0002 - DuckDB as the transformation engine

Status: accepted

## Context

Pipelines need an embeddable engine that reads relational databases, spreadsheets, Parquet and
HTTP sources, runs analytical SQL fast and does not need a server of its own.

## Decision

DuckDB executes every transformation node. Comparing two sources is a JOIN. SQL is the native
language of the platform; guided nodes (filter, group, join, pivot) generate SQL underneath, and
Python nodes cover what SQL cannot (executed in a sandboxed subprocess).

## Consequences

- Each node materialises its output as a table in a per-run DuckDB; the output is persisted as a
  Parquet snapshot.
- Memory, thread and spill limits are configurable per worker (`CFLOW_DUCKDB_*`) and derived from the
  container cgroup when not set.
- Values are always passed through native bind parameters; only validated identifiers are
  interpolated into SQL text.
