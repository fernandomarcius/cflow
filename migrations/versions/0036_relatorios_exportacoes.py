"""Reports layer: versioned declarative layouts + exports as the ARTIFACT of a run.

**`relatorio_layouts` / `relatorio_layout_versoes`** - the layout is versioned JSON in the same
mould as page/cube/view (`modelagem.versionado`): draft != published, optimistic lock, audited. It
is the ONLY thing this layer owns: column selection and order, label, cell format, sheet, stamp.
There is no SQL, formula, aggregation or total - the validation (`cflow_core.relatorios.layout`)
rejects whatever transforms data. Full grants: a layout is a catalog entry, it can be edited and
deleted.

**`relatorio_exportacoes`** - one row per requested artifact. `chave` is the hash of the tuple
`(execucao_id, no_id, layout_id, layout_versao, formato)`: same click, same bytes. The uniqueness is
PARTIAL (`estado <> 'falha'`), on purpose - two identical requests in flight or ready converge to
one record and one file, but an export that FAILED stays as evidence and the next attempt is a NEW
record (same principle as the ingestion retry and re-execution: reopening the previous one would
erase the failure). No DELETE for `cflow_app`: the export is part of the trace chain (report ->
execution -> node), and `Acao.EXPORTAR` enters the trail in the same transaction.

`gerado_por` null = requested by the platform (eager generation of the default layouts when the
execution completes), with `ator_tipo='sistema'` in the trail.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-05
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE relatorio_layouts (
      id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      nome                text NOT NULL,
      descricao           text,
      dono_id             uuid REFERENCES usuarios(id),
      versao_publicada_id uuid,
      sistema             boolean NOT NULL DEFAULT false,
      criado_em           timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE relatorio_layout_versoes (
      id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      layout_id    uuid NOT NULL REFERENCES relatorio_layouts(id),
      numero       int NOT NULL,
      definicao    jsonb NOT NULL,
      status       status_versao NOT NULL DEFAULT 'rascunho',
      nota_mudanca text,
      autor_id     uuid REFERENCES usuarios(id),
      criado_em    timestamptz NOT NULL DEFAULT now(),
      UNIQUE (layout_id, numero)
    )
    """,
    "CREATE INDEX idx_relatorio_layout_versoes ON relatorio_layout_versoes (layout_id, numero)",
    """
    CREATE TABLE relatorio_exportacoes (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      chave           text NOT NULL,
      execucao_id     uuid NOT NULL REFERENCES execucoes(id),
      no_id           text NOT NULL,
      layout_id       uuid NOT NULL REFERENCES relatorio_layouts(id),
      layout_versao   int NOT NULL,
      formato         text NOT NULL DEFAULT 'xlsx' CHECK (formato IN ('xlsx')),
      estado          text NOT NULL DEFAULT 'gerando'
                      CHECK (estado IN ('gerando', 'pronto', 'falha')),
      linhas_total    bigint,
      linhas_escritas bigint NOT NULL DEFAULT 0,
      bytes           bigint,
      arquivo_ref     text,
      nome_arquivo    text NOT NULL,
      gerado_por      uuid REFERENCES usuarios(id),
      criado_em       timestamptz NOT NULL DEFAULT now(),
      concluido_em    timestamptz,
      erro            text
    )
    """,
    # Idempotency: one artifact per tuple WHILE it has not failed. The failure stays recorded and
    # frees the key for the next attempt, which is a new record.
    "CREATE UNIQUE INDEX ux_relatorio_exportacoes_chave ON relatorio_exportacoes (chave) "
    "WHERE estado <> 'falha'",
    "CREATE INDEX idx_relatorio_exportacoes_exec ON relatorio_exportacoes (execucao_id, no_id)",
    "CREATE INDEX idx_relatorio_exportacoes_estado ON relatorio_exportacoes (estado, criado_em)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON relatorio_layouts TO cflow_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON relatorio_layout_versoes TO cflow_app",
    # The export is a trace artifact: it changes state (UPDATE), never disappears (no DELETE).
    "GRANT SELECT, INSERT, UPDATE ON relatorio_exportacoes TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP TABLE IF EXISTS relatorio_exportacoes",
        "DROP TABLE IF EXISTS relatorio_layout_versoes",
        "DROP TABLE IF EXISTS relatorio_layouts",
    ]:
        op.execute(c)
