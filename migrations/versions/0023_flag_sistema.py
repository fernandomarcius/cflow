"""`sistema` flag per entity (pipeline, cube, view, page, function library): a flagged item is
only visible/editable/executable by an admin in the Studio. Default false - only an admin turns it
on (admin-only PATCH route, audited). Does NOT affect the Portal (published pages remain
consumable) nor the internal resolution of chains (page->view->cube, pipeline-type source,
schedules).

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_TABELAS = ["pipelines", "cubos", "views", "paginas", "biblioteca_funcoes"]
DDL = [f"ALTER TABLE {t} ADD COLUMN sistema boolean NOT NULL DEFAULT false" for t in _TABELAS]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for t in _TABELAS:
        op.execute(f"ALTER TABLE {t} DROP COLUMN IF EXISTS sistema")
