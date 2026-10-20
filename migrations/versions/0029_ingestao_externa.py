"""External data ingestion - medallion lake, landing -> bronze.

Five tables + the `objeto` connection type (lake destination; the service-account credential goes
to the vault, never here). `ALTER TYPE ... ADD VALUE` cannot run in a transaction -> autocommit_block,
and it comes BEFORE the rest (the tables do not depend on it, but the block must close on its own).

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

DDL = [
    # --- External source: the declarative CONTRACT. Adding a source = inserting a row here. ---
    """
    CREATE TABLE ingestao_fontes (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL UNIQUE,
      dataset             text NOT NULL UNIQUE,
      classe              text NOT NULL,
      contrato            jsonb NOT NULL,
      destino_conexao_id  uuid REFERENCES conexoes(id),
      ativo               boolean NOT NULL DEFAULT true,
      -- `sistema`: source maintained by the PLATFORM (seeded from the catalog), not authored by a user.
      -- Same meaning as the flag in migration 0023: separates what operations manage from what
      -- someone registered by hand, so a re-seed does not overwrite people's work.
      sistema             boolean NOT NULL DEFAULT false,
      cadencia            text NOT NULL DEFAULT 'manual',
      proxima_em          timestamptz,
      criado_por          uuid REFERENCES usuarios(id),
      criado_em           timestamptz NOT NULL DEFAULT now(),
      atualizado_em       timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Scheduler queue by cadence (same pattern as `agendamentos`).
    "CREATE INDEX idx_ingestao_fontes_devidas ON ingestao_fontes (ativo, proxima_em)",
    """
    CREATE TABLE ingestao_execucoes (
      id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      fonte_id         uuid NOT NULL REFERENCES ingestao_fontes(id) ON DELETE CASCADE,
      status           text NOT NULL DEFAULT 'executando',
      origem           text NOT NULL DEFAULT 'manual',
      janela_inicio    date,
      janela_fim       date,
      lotes            int NOT NULL DEFAULT 0,
      lotes_novos      int NOT NULL DEFAULT 0,
      lotes_reusados   int NOT NULL DEFAULT 0,
      lotes_quarentena int NOT NULL DEFAULT 0,
      erro             text,
      criado_por       uuid REFERENCES usuarios(id),
      iniciada_em      timestamptz NOT NULL DEFAULT now(),
      concluida_em     timestamptz
    )
    """,
    "CREATE INDEX idx_ingestao_execucoes_fonte ON ingestao_execucoes (fonte_id, iniciada_em DESC)",
    # --- Batch = one SLICE of the window. The uniqueness (fonte, chave_lote, sha256) IS the idempotency: ---
    # re-running the same slice with the SAME payload collides here and becomes a no-op, without
    # rewriting landing or bronze. Without this index the guarantee would depend on a SELECT before
    # the INSERT (race).
    """
    CREATE TABLE ingestao_lotes (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      fonte_id           uuid NOT NULL REFERENCES ingestao_fontes(id) ON DELETE CASCADE,
      execucao_id        uuid REFERENCES ingestao_execucoes(id) ON DELETE SET NULL,
      chave_lote         text NOT NULL,
      sha256             text NOT NULL,
      bytes              bigint NOT NULL DEFAULT 0,
      linhas             bigint,
      etag               text,
      ultima_modificacao text,
      origem_url         text,
      landing_uri        text,
      bronze_uri         text,
      manifesto          jsonb,
      status             text NOT NULL DEFAULT 'landing',
      ingestion_date     date NOT NULL,
      criado_em          timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_ingestao_lote UNIQUE (fonte_id, chave_lote, sha256)
    )
    """,
    # Conditional GET: needs the ETag of the LAST batch of THAT slice (not the source's last batch).
    "CREATE INDEX idx_ingestao_lotes_fatia ON ingestao_lotes "
    "(fonte_id, chave_lote, criado_em DESC)",
    "CREATE INDEX idx_ingestao_lotes_execucao ON ingestao_lotes (execucao_id)",
    # --- Watermark: how far the source has been processed (one row per source). ---
    """
    CREATE TABLE ingestao_watermarks (
      fonte_id    uuid PRIMARY KEY REFERENCES ingestao_fontes(id) ON DELETE CASCADE,
      chave_lote  text NOT NULL,
      momento     timestamptz NOT NULL DEFAULT now(),
      detalhe     jsonb
    )
    """,
    # --- DLQ: download/decoding/parse/validation failure. The raw payload goes to the quarantine
    # bucket; here is the queryable record (a failed slice does not take down the window). ---
    """
    CREATE TABLE ingestao_quarentena (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      fonte_id     uuid NOT NULL REFERENCES ingestao_fontes(id) ON DELETE CASCADE,
      execucao_id  uuid REFERENCES ingestao_execucoes(id) ON DELETE SET NULL,
      chave_lote   text NOT NULL,
      etapa        text NOT NULL,
      codigo       text NOT NULL,
      mensagem     text NOT NULL,
      uri          text,
      sha256       text,
      criado_em    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX idx_ingestao_quarentena_fonte ON ingestao_quarentena (fonte_id, criado_em DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ingestao_fontes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ingestao_execucoes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ingestao_lotes TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ingestao_watermarks TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ingestao_quarentena TO cflow_app",
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tipo_conexao ADD VALUE IF NOT EXISTS 'objeto'")
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for t in (
        "ingestao_quarentena",
        "ingestao_watermarks",
        "ingestao_lotes",
        "ingestao_execucoes",
        "ingestao_fontes",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t}")
    # Postgres cannot remove an enum value; 'objeto' stays (irreversible).
