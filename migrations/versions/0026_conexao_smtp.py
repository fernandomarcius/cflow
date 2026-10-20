"""New connection type `smtp` (mail server for the `email` effect node).

Adds the value to the native `tipo_conexao` enum. ALTER TYPE ... ADD VALUE cannot run inside a
transaction -> uses autocommit_block. Keep in sync with `cflow_core.conexoes.service.TIPOS`.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tipo_conexao ADD VALUE IF NOT EXISTS 'smtp'")


def downgrade() -> None:
    # Postgres cannot remove an enum value; no-op (irreversible).
    pass
