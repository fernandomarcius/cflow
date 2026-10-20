"""Base schema - identity, connections and audit sections of the data model, with the
`ator_tipo` column on the audit trail. Explicit SQL.

asyncpg does not allow multiple commands per prepared statement, so each DDL command is
executed on its own.

Revision ID: 0001
Revises:
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = [
    # --- ENUMs ---
    "CREATE TYPE perfil AS ENUM ('admin', 'especialista', 'operador', 'auditor')",
    "CREATE TYPE nivel_permissao AS ENUM ('ver', 'executar', 'editar', 'administrar')",
    "CREATE TYPE tipo_sujeito AS ENUM ('usuario', 'grupo')",
    "CREATE TYPE tipo_conexao AS ENUM ('sqlserver', 'postgres', 'api_rest', 'arquivo')",
    # --- Section 1: identity and access ---
    """
    CREATE TABLE usuarios (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      email          text UNIQUE NOT NULL,
      nome           text NOT NULL,
      fonte_auth     text NOT NULL DEFAULT 'local',
      perfil         perfil NOT NULL,
      ativo          boolean NOT NULL DEFAULT true,
      mfa_habilitado boolean NOT NULL DEFAULT false,
      ultimo_acesso  timestamptz,
      criado_em      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE grupos (
      id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome      text UNIQUE NOT NULL,
      descricao text
    )
    """,
    """
    CREATE TABLE usuarios_grupos (
      usuario_id uuid REFERENCES usuarios(id),
      grupo_id   uuid REFERENCES grupos(id),
      PRIMARY KEY (usuario_id, grupo_id)
    )
    """,
    """
    CREATE TABLE permissoes (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      sujeito_tipo  tipo_sujeito NOT NULL,
      sujeito_id    uuid NOT NULL,
      recurso_tipo  text NOT NULL,
      recurso_id    uuid NOT NULL,
      nivel         nivel_permissao NOT NULL,
      concedido_por uuid REFERENCES usuarios(id),
      criado_em     timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX idx_perm_recurso ON permissoes (recurso_tipo, recurso_id)",
    "CREATE INDEX idx_perm_sujeito ON permissoes (sujeito_tipo, sujeito_id)",
    # --- Section 2: connections (config WITHOUT secrets; credencial_ref points to the vault) ---
    """
    CREATE TABLE conexoes (
      id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome           text UNIQUE NOT NULL,
      tipo           tipo_conexao NOT NULL,
      config         jsonb NOT NULL,
      credencial_ref text,
      criado_por     uuid REFERENCES usuarios(id),
      ativo          boolean NOT NULL DEFAULT true,
      criado_em      timestamptz NOT NULL DEFAULT now()
    )
    """,
    # --- Section 8: audit trail (APPEND-ONLY) + ator_tipo ---
    """
    CREATE TABLE auditoria (
      id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      ocorrido_em  timestamptz NOT NULL DEFAULT now(),
      ator_id      uuid REFERENCES usuarios(id),
      ator_tipo    text NOT NULL DEFAULT 'sistema'
                   CHECK (ator_tipo IN ('usuario', 'sistema', 'anonimo')),
      acao         text NOT NULL,
      recurso_tipo text NOT NULL,
      recurso_id   uuid,
      antes        jsonb,
      depois       jsonb,
      request_id   text,
      ip           inet,
      CONSTRAINT ck_ator_coerente CHECK (
        (ator_tipo = 'usuario' AND ator_id IS NOT NULL)
        OR (ator_tipo IN ('sistema', 'anonimo') AND ator_id IS NULL)
      )
    )
    """,
    "CREATE INDEX idx_audit_recurso ON auditoria (recurso_tipo, recurso_id)",
    "CREATE INDEX idx_audit_ator ON auditoria (ator_id, ocorrido_em)",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS auditoria",
    "DROP TABLE IF EXISTS conexoes",
    "DROP TABLE IF EXISTS permissoes",
    "DROP TABLE IF EXISTS usuarios_grupos",
    "DROP TABLE IF EXISTS grupos",
    "DROP TABLE IF EXISTS usuarios",
    "DROP TYPE IF EXISTS tipo_conexao",
    "DROP TYPE IF EXISTS tipo_sujeito",
    "DROP TYPE IF EXISTS nivel_permissao",
    "DROP TYPE IF EXISTS perfil",
]


def upgrade() -> None:
    for comando in UPGRADE:
        op.execute(comando)


def downgrade() -> None:
    for comando in DOWNGRADE:
        op.execute(comando)
