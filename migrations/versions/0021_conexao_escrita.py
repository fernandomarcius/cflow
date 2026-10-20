"""`permitir_escrita` flag per connection: lets EFFECT nodes (escrita_sql / http) use the
connection as a destination. Default false - only an admin turns it on (admin-only PATCH route,
audited). Without the flag, publishing/running a pipeline with an effect node fails. Applies to
both types (http even for GET).

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE conexoes ADD COLUMN permitir_escrita boolean NOT NULL DEFAULT false",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    op.execute("ALTER TABLE conexoes DROP COLUMN IF EXISTS permitir_escrita")
