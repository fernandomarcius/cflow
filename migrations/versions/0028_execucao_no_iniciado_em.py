"""Execution node: stores WHEN it started, not just when it finished.

`execucao_nos` only got a row at the OUTCOME (completed/failed). While the execution runs, the
node running RIGHT NOW was indistinguishable from one that had not started yet - the detail screen
showed "waiting" for both. In a 24-node pipeline with 20-minute stages, that is the difference
between "it is moving" and "it is stuck".

With `iniciado_em` the screen shows which node is in progress and for how long.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execucao_nos",
        sa.Column("iniciado_em", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execucao_nos", "iniciado_em")
