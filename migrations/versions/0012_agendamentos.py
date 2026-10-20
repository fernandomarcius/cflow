"""Pipeline execution schedule. The worker periodically fires the ones that are due.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE agendamentos (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      pipeline_id        uuid NOT NULL REFERENCES pipelines(id),
      intervalo_segundos int NOT NULL,
      ativo              boolean NOT NULL DEFAULT true,
      proxima_em         timestamptz NOT NULL,
      criado_por         uuid REFERENCES usuarios(id),
      criado_em          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX idx_agendamentos_devidos ON agendamentos (ativo, proxima_em)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON agendamentos TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agendamentos")
