# ADR 0008 - A pipeline can be the source of another pipeline

Status: accepted

## Decision

A `fonte` node accepts `origem: "pipeline"` with `{pipeline_id, no_id, versao?}`: it reads the
Parquet snapshot of the referenced execution of another pipeline, through the same generic source
node (no special component). The dependencies between pipelines form a DAG resolved topologically,
with cycle detection. When a chain is triggered, every source reads the exact execution of its
upstream produced in the same run, which guarantees freshness.

## Consequences

- Shared logic is built once and consumed by many pipelines instead of being copied.
- Preview of a downstream pipeline needs at least one run of the upstream (otherwise `sem_execucao`).
- A "run" can be a chain: the requested pipeline is the root, the others are upstream members, and
  the run list paginates by chain.
