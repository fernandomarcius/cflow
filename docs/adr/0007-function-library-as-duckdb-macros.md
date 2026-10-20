# ADR 0007 - Reusable transformation logic is a versioned library of DuckDB macros

Status: accepted

## Context

The same normalisation rules (identifier cleaning, date handling, filters) were being re-coded in
the SQL of many nodes, drifting apart over time.

## Decision

A **function library** is a versioned entity (draft vs published, audited) whose JSON definition
compiles to `CREATE OR REPLACE MACRO` statements. Macros are loaded into **every** DuckDB
connection, both in execution and in preview, before any node runs. A pipeline lists the libraries
it uses; a name collision between libraries loaded together is an error.

## Consequences

- Each run pins the published library versions it used; the pin is part of the reuse fingerprint,
  so republishing a library invalidates the snapshots of the nodes that call it.
- Lookup-dependent logic stays a join in the graph, not a macro.
- The editor can evaluate a macro against sample values (`POST /funcoes/{id}/avaliar`).
