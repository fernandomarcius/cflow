# ADR 0006 - Definitions are versioned JSON (draft is not published)

Status: accepted

## Decision

Pipelines, cubes, views, pages, report layouts and function libraries are JSON documents with a
numbered, immutable version, an author and a change note. A draft is mutable (optimistic lock); a
published version never changes. A run references the exact version it executed.

## Consequences

- Reverting is publishing an older definition as a new version.
- Every definition needs a stable schema and, when it evolves, a migration path.
- Parameters (`:name`) are bound natively; a parameter flagged as secret is redacted in everything
  that persists (run record, audit diff, logs).
