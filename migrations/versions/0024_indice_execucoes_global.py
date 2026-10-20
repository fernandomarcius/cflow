"""Index for the GLOBAL execution list (all pipelines on a single screen).

`idx_exec_pipeline (pipeline_id, iniciada_em)` only helps when the pipeline is fixed; the "all
executions" listing orders by `iniciada_em DESC, id DESC` without a pipeline scope and needs its
own index (btree scanned backwards). `id` at the end keeps pagination stable when two executions
share the same `iniciada_em`.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX idx_exec_iniciada ON execucoes (iniciada_em, id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_exec_iniciada")
