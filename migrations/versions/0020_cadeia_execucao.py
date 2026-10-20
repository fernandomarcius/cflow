"""Execution chain: identifies executions that belong to the SAME chained-execution trigger
(pipeline DAG), enabling `fonte` reuse between sibling pipelines of the chain (same as_of) without
affecting freshness across different chains.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE execucoes ADD COLUMN cadeia_id uuid NULL",
    "CREATE INDEX idx_execucoes_cadeia ON execucoes (cadeia_id) WHERE cadeia_id IS NOT NULL",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP INDEX IF EXISTS idx_execucoes_cadeia",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS cadeia_id",
    ]:
        op.execute(c)
