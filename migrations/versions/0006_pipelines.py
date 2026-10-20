"""Pipelines and versions (section 3 of the data model). Definition = versioned JSON.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

DDL = [
    "CREATE TYPE status_versao AS ENUM ('rascunho', 'publicada', 'arquivada')",
    """
    CREATE TABLE pipelines (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL,
      descricao           text,
      dono_id             uuid REFERENCES usuarios(id),
      versao_publicada_id uuid,
      criado_em           timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE pipeline_versoes (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      pipeline_id  uuid NOT NULL REFERENCES pipelines(id),
      numero       int NOT NULL,
      definicao    jsonb NOT NULL,
      status       status_versao NOT NULL DEFAULT 'rascunho',
      nota_mudanca text,
      autor_id     uuid REFERENCES usuarios(id),
      criado_em    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (pipeline_id, numero)
    )
    """,
    "CREATE INDEX idx_pipe_versoes ON pipeline_versoes (pipeline_id, numero)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON pipelines TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON pipeline_versoes TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP TABLE IF EXISTS pipeline_versoes",
        "DROP TABLE IF EXISTS pipelines",
        "DROP TYPE IF EXISTS status_versao",
    ]:
        op.execute(c)
