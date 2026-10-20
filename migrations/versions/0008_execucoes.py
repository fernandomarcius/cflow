"""Executions and per-node snapshots. Trace chain: execution -> node -> snapshot.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

DDL = [
    "CREATE TYPE status_execucao AS ENUM ('executando', 'concluida', 'falha')",
    """
    CREATE TABLE execucoes (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      pipeline_id   uuid NOT NULL REFERENCES pipelines(id),
      versao_numero int NOT NULL,
      status        status_execucao NOT NULL DEFAULT 'executando',
      erro          text,
      iniciada_por  uuid REFERENCES usuarios(id),
      iniciada_em   timestamptz NOT NULL DEFAULT now(),
      concluida_em  timestamptz
    )
    """,
    """
    CREATE TABLE execucao_nos (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      execucao_id  uuid NOT NULL REFERENCES execucoes(id) ON DELETE CASCADE,
      no_id        text NOT NULL,
      status       status_execucao NOT NULL DEFAULT 'executando',
      linhas       bigint,
      hash         text,
      snapshot     text,
      duracao_ms   int,
      erro         text,
      concluido_em timestamptz,
      UNIQUE (execucao_id, no_id)
    )
    """,
    "CREATE INDEX idx_exec_pipeline ON execucoes (pipeline_id, iniciada_em)",
    "CREATE INDEX idx_exec_nos ON execucao_nos (execucao_id)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON execucoes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON execucao_nos TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP TABLE IF EXISTS execucao_nos",
        "DROP TABLE IF EXISTS execucoes",
        "DROP TYPE IF EXISTS status_execucao",
    ]:
        op.execute(c)
