"""Covering, ordered index for the fingerprint reuse lookup.

`idx_exec_nos_fp` (migration 0009) is a btree on `fingerprint` only. The two hot reuse queries -
the chain candidate (`engine._SQL_CAND_CADEIA`) and the snapshot of a previous execution
(`snapshots.py`) - also filter by `status` and `snapshot IS NOT NULL`, order by
`concluido_em DESC` and read `snapshot`, `hash` and `linhas`. With the current index, Postgres
scans every row of that fingerprint, filters, fetches the heap for each one and sorts.

**A benchmark does not show this**, and that is exactly why the migration exists: the benchmark
database starts empty on every run, so there is ~1 row per fingerprint and any plan is instant. In
production `execucao_nos` grows one row per node per execution, and the number of rows per
fingerprint grows with the history. The cost is O(past executions with that fingerprint), paid once
per node on every run, and it degrades slowly enough that nobody connects the dots.

PARTIAL index (only what can be reused: completed and with a snapshot) with `INCLUDE`, so the
lookup is index-only and already ordered. The old one stays: it still serves queries that do not
filter by status.
"""

from __future__ import annotations

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_exec_nos_reuso
            ON execucao_nos (fingerprint, concluido_em DESC)
            INCLUDE (snapshot, hash, linhas)
            WHERE status = 'concluida' AND snapshot IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_exec_nos_reuso")
