"""Medallion layer on the pipeline: `camada` in (bronze, silver, gold), NULL = no layer.

**One column, not three booleans.** The medallion layers are mutually exclusive: a pipeline is not
bronze AND silver at the same time - it produces ONE layer. With three booleans the database would
accept `bronze=true, silver=true` (and `gold=true` on the same item), and every reader would have
to decide on its own which one wins; a CHECK on a single column makes the invalid state
inexpressible. A new layer (`platinum`?) is one more value in the CHECK, not a migration that adds
a column.

**`sistema` is ORTHOGONAL and stays as it is** (migration 0023). `sistema` answers "who CAN see"
(admin-only in the Studio); `camada` answers "in which TAB it appears". A silver pipeline can be
visible to every specialist, and a pipeline without a layer can be admin-only. Mixing both in one
field would make the Bronze tab hide the pipeline from whoever is entitled to see it.

`NULL` passes the CHECK without an extra clause (`NULL IN (...)` is NULL, not false) - that is why
there is no `camada IS NULL OR`. The `pipelines` grant (0006) is per table: a new column comes for
free.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipelines ADD COLUMN camada text")
    op.execute(
        "ALTER TABLE pipelines ADD CONSTRAINT pipelines_camada_chk "
        "CHECK (camada IN ('bronze','silver','gold'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pipelines DROP CONSTRAINT IF EXISTS pipelines_camada_chk")
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS camada")
