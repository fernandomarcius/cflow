"""Incremental snapshot reuse: node fingerprint + reuse flag.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE execucao_nos ADD COLUMN IF NOT EXISTS fingerprint text")
    op.execute(
        "ALTER TABLE execucao_nos ADD COLUMN IF NOT EXISTS reaproveitado boolean NOT NULL DEFAULT false"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_exec_nos_fp ON execucao_nos (fingerprint)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_exec_nos_fp")
    op.execute("ALTER TABLE execucao_nos DROP COLUMN IF EXISTS reaproveitado")
    op.execute("ALTER TABLE execucao_nos DROP COLUMN IF EXISTS fingerprint")
