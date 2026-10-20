"""Iceberg JDBC catalog for the silver layer.

The two tables are the **standard schema of the Iceberg JDBC catalog** - not our invention. That is
why the same catalog works for PyIceberg to write and for a query engine to read with
`iceberg.catalog.type=jdbc`, pointing both at this Postgres.

They are created here, and not by PyIceberg's `init_catalog_tables`, because the runtime connects
as `cflow_app`, which **has no DDL** - DDL belongs to `cflow_owner`, via migration. Letting the
library create tables in production breaks that separation, which is a security rule of the
project.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iceberg_tables",
        sa.Column("catalog_name", sa.String(255), nullable=False),
        sa.Column("table_namespace", sa.String(255), nullable=False),
        sa.Column("table_name", sa.String(255), nullable=False),
        sa.Column("metadata_location", sa.String(1000)),
        sa.Column("previous_metadata_location", sa.String(1000)),
        sa.PrimaryKeyConstraint("catalog_name", "table_namespace", "table_name"),
    )
    op.create_table(
        "iceberg_namespace_properties",
        sa.Column("catalog_name", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("property_key", sa.String(255), nullable=False),
        sa.Column("property_value", sa.String(1000), nullable=False),
        sa.PrimaryKeyConstraint("catalog_name", "namespace", "property_key"),
    )
    # The runtime needs DML here: publishing a snapshot means swapping `metadata_location`. Without
    # UPDATE and DELETE the catalog would be read-only and publishing would fail on the second snapshot.
    for tabela in ("iceberg_tables", "iceberg_namespace_properties"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tabela} TO cflow_app")


def downgrade() -> None:
    op.drop_table("iceberg_namespace_properties")
    op.drop_table("iceberg_tables")
