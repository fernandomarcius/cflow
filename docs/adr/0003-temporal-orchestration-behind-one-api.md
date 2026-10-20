# ADR 0003 - Temporal orchestrates execution, hidden behind a single API

Status: accepted

## Context

A run is a graph of nodes with retries, partial failures, long-running extractions and scheduled
triggers. Writing that orchestration by hand re-implements durable execution badly.

## Decision

Each node of a published pipeline is an activity of a Temporal workflow (`ExecutarPipeline` for one
pipeline, `ExecutarDAG` for a chain of pipelines). Ingestion and report generation use their own
task queues, so a long backfill cannot starve pipeline runs. The REST API is the only entry point:
the web UI and any other client talk to the API, never to Temporal or DuckDB directly, and Temporal
vocabulary (workflow, activity, task queue) never leaks to users.

## Consequences

- Retries, immutable run history and replay come for free.
- The control plane is PostgreSQL: run status, node status, snapshots and audit live in the
  metadata database; Temporal is replaceable infrastructure behind the API.
- Orphan runs (a workflow that died before writing its outcome) are closed by a reconciliation job,
  which only acts when Temporal answered "unknown workflow" (never on a connection failure).
- With `CFLOW_USAR_TEMPORAL=false` the same code path runs inline, which is how unit tests work.
