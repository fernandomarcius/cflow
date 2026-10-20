"""Settings table (key/value) - technical log retention policy.

The audit trail stays permanent; this table only holds operational parameters.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE configuracoes (
      chave        text PRIMARY KEY,
      valor        text NOT NULL,
      atualizado_em timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON configuracoes TO cflow_app",
    "INSERT INTO configuracoes (chave, valor) VALUES ('log_retencao_dias', '90') "
    "ON CONFLICT (chave) DO NOTHING",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS configuracoes")
