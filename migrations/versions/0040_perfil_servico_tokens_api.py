"""`servico` profile and API tokens for service accounts.

The account used by an external job-queue adapter signed in with e-mail and password - and, with
the RBAC of the time, was a full `especialista` (it created its own pipelines). The `servico`
profile triggers and follows executions without authoring, and signs in only via API token:
opaque, prefixed `cfs_`, SHA-256 in the database (the token already has 256 bits), mandatory
expiry, revocable, shown once.

`ALTER TYPE ... ADD VALUE` runs inside the migration transaction since Postgres 12, as long as the
new value is not USED in it - nothing below uses it.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE perfil ADD VALUE IF NOT EXISTS 'servico'")
    op.execute(
        """
        CREATE TABLE tokens_api (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          usuario_id    uuid NOT NULL REFERENCES usuarios(id),
          nome          text NOT NULL,
          prefixo       text NOT NULL,
          token_hash    text NOT NULL UNIQUE,
          criado_em     timestamptz NOT NULL DEFAULT now(),
          criado_por    uuid NOT NULL REFERENCES usuarios(id),
          expira_em     timestamptz NOT NULL,
          ultimo_uso_em timestamptz,
          revogado_em   timestamptz,
          revogado_por  uuid REFERENCES usuarios(id)
        )
        """
    )
    op.execute("CREATE INDEX idx_tokens_api_usuario ON tokens_api (usuario_id)")
    # No DELETE: a revoked token stays as evidence that it existed (the trail points to it).
    op.execute("GRANT SELECT, INSERT, UPDATE ON tokens_api TO cflow_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tokens_api")
    # The 'servico' value of the `perfil` ENUM stays: Postgres cannot remove an enum value, and an
    # account with that profile would have to be migrated first - an operator decision, not the
    # downgrade's.
