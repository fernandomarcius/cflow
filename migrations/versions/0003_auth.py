"""Authentication - senha_hash + sessoes + tokens_recuperacao + tentativas_login + admin bootstrap.

Explicit SQL (individual statements; asyncpg does not accept multi-command). The admin's Argon2id
hash is computed HERE from CFLOW_ADMIN_PASSWORD; the plaintext password is never persisted.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-17
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS senha_hash text",
    """
    CREATE TABLE sessoes (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      usuario_id          uuid NOT NULL REFERENCES usuarios(id),
      token_hash          text NOT NULL UNIQUE,
      criada_em           timestamptz NOT NULL DEFAULT now(),
      ultima_atividade_em timestamptz NOT NULL DEFAULT now(),
      expira_em           timestamptz NOT NULL,
      revogada_em         timestamptz
    )
    """,
    "CREATE INDEX idx_sessoes_token ON sessoes (token_hash)",
    """
    CREATE TABLE tokens_recuperacao (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      usuario_id  uuid NOT NULL REFERENCES usuarios(id),
      token_hash  text NOT NULL,
      criado_em   timestamptz NOT NULL DEFAULT now(),
      expira_em   timestamptz NOT NULL,
      usado_em    timestamptz
    )
    """,
    "CREATE INDEX idx_recup_token ON tokens_recuperacao (token_hash)",
    """
    CREATE TABLE tentativas_login (
      id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      email_tentado text NOT NULL,
      ip            inet,
      ocorrido_em   timestamptz NOT NULL DEFAULT now(),
      sucesso       boolean NOT NULL
    )
    """,
    "CREATE INDEX idx_tentativas ON tentativas_login (email_tentado, ip, ocorrido_em)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON sessoes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tokens_recuperacao TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tentativas_login TO cflow_app",
    "GRANT USAGE, SELECT ON SEQUENCE tentativas_login_id_seq TO cflow_app",
]


def upgrade() -> None:
    for comando in DDL:
        op.execute(comando)

    # Admin bootstrap (idempotent). Password comes from env; Argon2id hash computed here.
    email = os.environ.get("CFLOW_ADMIN_EMAIL", "admin@example.com")
    senha = os.environ.get("CFLOW_ADMIN_PASSWORD")
    if email and senha:
        from argon2 import PasswordHasher

        senha_hash = PasswordHasher().hash(senha)
        conn = op.get_bind()
        conn.execute(
            sa.text(
                "INSERT INTO usuarios (email, nome, perfil, senha_hash) "
                "VALUES (:email, 'Administrador', 'admin', :h) "
                "ON CONFLICT (email) DO NOTHING"
            ),
            {"email": email, "h": senha_hash},
        )


def downgrade() -> None:
    for comando in [
        "DROP TABLE IF EXISTS tentativas_login",
        "DROP TABLE IF EXISTS tokens_recuperacao",
        "DROP TABLE IF EXISTS sessoes",
        "ALTER TABLE usuarios DROP COLUMN IF EXISTS senha_hash",
    ]:
        op.execute(comando)
