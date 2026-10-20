"""Durable `efeito_disparado` mark on `execucao_nos`: closes the window between the EXTERNAL commit
of an effect node (escrita_sql/http) and the 'concluida' upsert. Written in its own transaction
BEFORE the effect; the failure mark (except) does NOT erase it. On a redelivery/retry, if the mark
exists and the node did not complete, the effect MAY have committed externally -> non-idempotent
strategies (append/sql) are REFUSED (fail-safe) instead of duplicating; idempotent ones
(upsert/truncate_insert) may repeat.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE execucao_nos ADD COLUMN efeito_disparado boolean NOT NULL DEFAULT false",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    op.execute("ALTER TABLE execucao_nos DROP COLUMN IF EXISTS efeito_disparado")
