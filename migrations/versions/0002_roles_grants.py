"""Roles and grants - enforces append-only at the database level.

- cflow_owner: schema owner; the role that connects to run the migrations (created by the
  Postgres provisioning / POSTGRES_USER). Not recreated here.
- cflow_app: runtime role of the API/worker. Full DML on the business tables; on `auditoria`
  only SELECT/INSERT (UPDATE/DELETE revoked).

The cflow_app password comes from the CFLOW_APP_PASSWORD env var (never hardcoded). It is passed
to the database via set_config (safe bind) and applied with format(%L) - no string concatenation.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-17
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABELAS_NEGOCIO = ("usuarios", "grupos", "usuarios_grupos", "permissoes", "conexoes")


def upgrade() -> None:
    senha = os.environ.get("CFLOW_APP_PASSWORD")
    if not senha:
        raise RuntimeError("CFLOW_APP_PASSWORD is not set - required to create the cflow_app role.")

    conn = op.get_bind()
    # safe bind: injects the password into a session GUC without string concatenation.
    conn.execute(sa.text("SELECT set_config('app.new_app_password', :p, false)"), {"p": senha})

    # create/update cflow_app idempotently
    op.execute("""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cflow_app') THEN
            EXECUTE format('CREATE ROLE cflow_app LOGIN PASSWORD %L',
                           current_setting('app.new_app_password'));
          ELSE
            EXECUTE format('ALTER ROLE cflow_app LOGIN PASSWORD %L',
                           current_setting('app.new_app_password'));
          END IF;
        END $$;
    """)

    # schema access
    op.execute("GRANT USAGE ON SCHEMA public TO cflow_app;")

    # full DML on the business tables
    for tabela in TABELAS_NEGOCIO:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tabela} TO cflow_app;")

    # audit: APPEND-ONLY - only SELECT/INSERT; UPDATE/DELETE explicitly revoked
    op.execute("GRANT SELECT, INSERT ON auditoria TO cflow_app;")
    op.execute("REVOKE UPDATE, DELETE ON auditoria FROM cflow_app;")


def downgrade() -> None:
    for tabela in (*TABELAS_NEGOCIO, "auditoria"):
        op.execute(f"REVOKE ALL ON {tabela} FROM cflow_app;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM cflow_app;")
    # the role is not dropped automatically (it may be in use by connections/objects).
