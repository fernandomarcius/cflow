"""Credential vault - encrypted secrets, kept apart from the metadata.

The `conexoes` table (section 2) already exists (0001) and only holds secret-free config +
credencial_ref. Here we create the vault, whose content is encrypted by the application (Fernet)
before being persisted.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE cofre_credenciais (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      segredo_cifrado bytea NOT NULL,
      criado_em       timestamptz NOT NULL DEFAULT now(),
      atualizado_em   timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON cofre_credenciais TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cofre_credenciais")
