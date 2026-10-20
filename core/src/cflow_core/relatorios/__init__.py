"""Reporting layer: a declarative layout over a node's snapshot, materialized once as an immutable
run artifact. The layer ORGANIZES (columns, label, format, sheet, stamp) and never transforms
data -- what it refuses to do is encoded in `layout.validar`."""
