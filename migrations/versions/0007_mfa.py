"""MFA - reference to the TOTP secret in the vault. `mfa_habilitado` already exists (section 1).

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS mfa_credencial_ref uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE usuarios DROP COLUMN IF EXISTS mfa_credencial_ref")
