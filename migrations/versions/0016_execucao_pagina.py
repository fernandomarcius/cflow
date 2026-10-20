"""Execution origin: which PAGE triggered the run (the user is already `iniciada_por`).

Lets the portal/dashboard show where each execution came from (page + user).

Revision ID: 0016
Revises: 0014
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0014"
branch_labels = None
depends_on = None

DDL = [
    # Page that originated the run; SET NULL if the page is removed (the execution stays auditable).
    "ALTER TABLE execucoes ADD COLUMN pagina_id uuid REFERENCES paginas(id) ON DELETE SET NULL",
    "CREATE INDEX idx_exec_pagina ON execucoes (pagina_id)",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP INDEX IF EXISTS idx_exec_pagina",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS pagina_id",
    ]:
        op.execute(c)
