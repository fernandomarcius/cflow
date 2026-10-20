"""Function library: versioned definitions of reusable DuckDB MACROs (draft != published,
audited). Compiled into CREATE OR REPLACE MACRO and loaded into every DuckDB session (execution +
preview). The execution pins the versions used in `execucoes.bibliotecas_versoes`, part of the
trace chain of the stamped report.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE biblioteca_funcoes (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL,
      descricao           text,
      dono_id             uuid REFERENCES usuarios(id),
      versao_publicada_id uuid,
      criado_em           timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE biblioteca_funcoes_versoes (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      biblioteca_id uuid NOT NULL REFERENCES biblioteca_funcoes(id),
      numero        int NOT NULL,
      definicao     jsonb NOT NULL,
      status        status_versao NOT NULL DEFAULT 'rascunho',
      nota_mudanca  text,
      autor_id      uuid REFERENCES usuarios(id),
      criado_em     timestamptz NOT NULL DEFAULT now(),
      UNIQUE (biblioteca_id, numero)
    )
    """,
    "CREATE INDEX idx_biblioteca_funcoes_versoes "
    "ON biblioteca_funcoes_versoes (biblioteca_id, numero)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON biblioteca_funcoes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON biblioteca_funcoes_versoes TO cflow_app",
    # pin of the library versions used by the execution (trace chain of the stamped report)
    "ALTER TABLE execucoes ADD COLUMN bibliotecas_versoes jsonb NOT NULL DEFAULT '[]'::jsonb",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS bibliotecas_versoes",
        "DROP TABLE IF EXISTS biblioteca_funcoes_versoes",
        "DROP TABLE IF EXISTS biblioteca_funcoes",
    ]:
        op.execute(c)
