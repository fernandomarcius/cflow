"""Schedule: stores WHEN the last trigger happened, not just the outcome.

The table recorded `ultimo_status` ("disparado", "erro", "vazio") and discarded the instant. On
screen, a "FAILED" without a date does not say whether the problem is from now or from months ago -
and in an auditable product the time is part of the fact.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agendamentos",
        sa.Column("ultimo_disparo_em", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agendamentos", "ultimo_disparo_em")
