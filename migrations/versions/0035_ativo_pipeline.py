"""A pipeline can be deactivated: `ativo boolean NOT NULL DEFAULT true`.

**Blocks EXECUTION, not editing.** There are external triggers (cron scheduler, "run-now" API,
re-execute) calling pipelines outside the UI - deactivating must hold for ALL of them, so the
check lives in the single choke point (`engine.criar_execucao`), not in each route that fires an
execution. A draft stays editable and publishable while the pipeline is deactivated: "ativo" is
about RUNNING, not about existing.

Same pattern already used elsewhere in the schema (`usuarios.ativo`, `agendamentos.ativo`,
`ingestao_fontes.ativo`, see 0029) - a plain boolean, no CHECK, default `true` so existing
pipelines are not silently deactivated by the migration. The `pipelines` grant (0006) is per
table: a new column comes for free, same note as 0031/0033.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipelines ADD COLUMN ativo boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS ativo")
