"""Modeling: cubes (dimensions + named measures) and views (pivot). Versioned JSON.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE cubos (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL,
      descricao           text,
      dono_id             uuid REFERENCES usuarios(id),
      versao_publicada_id uuid,
      criado_em           timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE cubo_versoes (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      cubo_id      uuid NOT NULL REFERENCES cubos(id),
      numero       int NOT NULL,
      definicao    jsonb NOT NULL,
      status       status_versao NOT NULL DEFAULT 'rascunho',
      nota_mudanca text,
      autor_id     uuid REFERENCES usuarios(id),
      criado_em    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (cubo_id, numero)
    )
    """,
    """
    CREATE TABLE views (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL,
      descricao           text,
      dono_id             uuid REFERENCES usuarios(id),
      versao_publicada_id uuid,
      criado_em           timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE view_versoes (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      view_id      uuid NOT NULL REFERENCES views(id),
      numero       int NOT NULL,
      definicao    jsonb NOT NULL,
      status       status_versao NOT NULL DEFAULT 'rascunho',
      nota_mudanca text,
      autor_id     uuid REFERENCES usuarios(id),
      criado_em    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (view_id, numero)
    )
    """,
    "CREATE INDEX idx_cubo_versoes ON cubo_versoes (cubo_id, numero)",
    "CREATE INDEX idx_view_versoes ON view_versoes (view_id, numero)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON cubos TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON cubo_versoes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON views TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON view_versoes TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP TABLE IF EXISTS view_versoes",
        "DROP TABLE IF EXISTS views",
        "DROP TABLE IF EXISTS cubo_versoes",
        "DROP TABLE IF EXISTS cubos",
    ]:
        op.execute(c)
