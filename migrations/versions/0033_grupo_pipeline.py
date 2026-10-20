"""Pipeline group: `grupo` in (load, reference, export), NULL = no group.

**A new column, and not one more value in `camada`.** The two answer different questions:
`camada` says WHICH medallion LAYER the pipeline produces (bronze/silver/gold) and `grupo` says
WHICH SET it belongs to. A pipeline can be both at once, and `camada` holds a single value: putting
the group there would take the pipeline out of its layer tab, where it legitimately belongs.

CLOSED vocabulary, for the same reason as 0031: a new group is one more value in the CHECK, not
free text. Free text would become "load_x", "load-x" and "loadX" in the same listing, each opening
a tab of its own.

`sistema` remains orthogonal to both: it answers "who CAN see".

`NULL` passes the CHECK without an extra clause (`NULL IN (...)` is NULL, not false). The
`pipelines` grant (0006) is per table: a new column comes for free.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipelines ADD COLUMN grupo text")
    op.execute(
        "ALTER TABLE pipelines ADD CONSTRAINT pipelines_grupo_chk CHECK (grupo IN ('load', 'reference', 'export'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pipelines DROP CONSTRAINT IF EXISTS pipelines_grupo_chk")
    op.execute("ALTER TABLE pipelines DROP COLUMN IF EXISTS grupo")
