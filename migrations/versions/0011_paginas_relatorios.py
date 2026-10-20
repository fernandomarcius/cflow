"""Pages + stamped reports. A page = versioned JSON of blocks that reference views (it does not
compute). A report = IMMUTABLE stamp (only SELECT/INSERT for cflow_app).

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE paginas (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL,
      descricao           text,
      dono_id             uuid REFERENCES usuarios(id),
      versao_publicada_id uuid,
      criado_em           timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE pagina_versoes (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      pagina_id    uuid NOT NULL REFERENCES paginas(id),
      numero       int NOT NULL,
      definicao    jsonb NOT NULL,
      status       status_versao NOT NULL DEFAULT 'rascunho',
      nota_mudanca text,
      autor_id     uuid REFERENCES usuarios(id),
      criado_em    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (pagina_id, numero)
    )
    """,
    """
    CREATE TABLE relatorios (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      pagina_id     uuid NOT NULL REFERENCES paginas(id),
      pagina_versao int NOT NULL,
      gerado_por    uuid REFERENCES usuarios(id),
      gerado_em     timestamptz NOT NULL DEFAULT now(),
      filtros       jsonb,
      conteudo      jsonb NOT NULL,
      trace         jsonb NOT NULL
    )
    """,
    "CREATE INDEX idx_pagina_versoes ON pagina_versoes (pagina_id, numero)",
    "CREATE INDEX idx_relatorios ON relatorios (pagina_id, gerado_em)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON paginas TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON pagina_versoes TO cflow_app",
    # a report is an immutable stamp: only SELECT/INSERT (no UPDATE/DELETE)
    "GRANT SELECT, INSERT ON relatorios TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP TABLE IF EXISTS relatorios",
        "DROP TABLE IF EXISTS pagina_versoes",
        "DROP TABLE IF EXISTS paginas",
    ]:
        op.execute(c)
