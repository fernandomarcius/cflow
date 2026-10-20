"""New connection type `api_query` (SQL-over-REST gateway).

Adds the value to the native `tipo_conexao` enum. ALTER TYPE ... ADD VALUE cannot run inside a
transaction -> uses autocommit_block. Keep in sync with `cflow_core.conexoes.service.TIPOS`.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tipo_conexao ADD VALUE IF NOT EXISTS 'api_query'")


def downgrade() -> None:
    # Postgres cannot remove an enum value; no-op (irreversible).
    pass
