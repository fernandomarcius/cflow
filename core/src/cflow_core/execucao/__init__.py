"""Production execution: runs the published graph over the FULL data in DuckDB, persists one Parquet
snapshot per node and records the execution trail. Same SQL as the preview; only the data changes
(sample -> full).

The functions are meant to be Temporal *activity* bodies (one node = one activity); Temporal
orchestration wraps them separately.
"""
