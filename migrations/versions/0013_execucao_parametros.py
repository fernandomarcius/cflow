"""Execution runtime parameters: chosen values (e.g. project/month) that filter the source.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE execucoes ADD COLUMN parametros jsonb NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE execucoes DROP COLUMN IF EXISTS parametros")
