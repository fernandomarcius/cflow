# ADR 0005 - Sampled preview is separate from production execution

Status: accepted

## Decision

Clicking a node in the editor recomputes its output over a **sample** of the sources in a
session-scoped DuckDB, without going through Temporal. A production run persists the **full**
snapshot of every node.

## Rationale

Authoring needs feedback in seconds, which is incompatible with full orchestration. Full snapshots
give forensic debugging: open any historical run and inspect the real data each step produced.

## Consequences

- Two execution paths must stay coherent: the same SQL and the same node semantics, with different
  data. Preview never publishes and never triggers side effects (writes, HTTP calls, e-mail).
- Snapshots have a retention policy (`CFLOW_SNAPSHOT_RETENCAO_DIAS`) and orphan snapshots are
  purged by a maintenance routine.
