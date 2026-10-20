"""Execution origin (manual x scheduled) + pointer to the schedule that fired it.

Basis for the daily report cache and for the rich schedule: it allows finding "execution completed
today with these same parameters" and telling manual from scheduled runs apart.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE execucoes ADD COLUMN origem text NOT NULL DEFAULT 'manual'",
    "ALTER TABLE execucoes ADD CONSTRAINT execucoes_origem_chk CHECK (origem IN ('manual', 'agendado'))",
    # Which schedule fired it (NULL = manual); SET NULL if the schedule is removed.
    "ALTER TABLE execucoes ADD COLUMN agendamento_id uuid REFERENCES agendamentos(id) ON DELETE SET NULL",
    # Daily lookup: filters by pipeline + page and orders by iniciada_em.
    "CREATE INDEX idx_exec_dia ON execucoes (pipeline_id, pagina_id, iniciada_em)",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP INDEX IF EXISTS idx_exec_dia",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS agendamento_id",
        "ALTER TABLE execucoes DROP CONSTRAINT IF EXISTS execucoes_origem_chk",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS origem",
    ]:
        op.execute(c)
