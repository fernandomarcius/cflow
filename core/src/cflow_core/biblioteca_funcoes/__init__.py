"""Function library: versioned definitions of reusable DuckDB MACROs.

Each library is versioned JSON (draft != published, audited) that compiles to
`CREATE OR REPLACE MACRO`, loadable into every DuckDB session (execution + preview). There may
be several libraries; each pipeline references the ones it uses. Reuses the generic CRUD in
`cflow_core.modelagem.versionado` (no versioning logic is duplicated).
"""
