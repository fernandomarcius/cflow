"""Per-slice ingestion log: the record that was missing for history, log and retry.

Three needs - complete execution history per source, execution log, and per-slice retry - are the
SAME gap seen from different angles: all depend on a record of what happened to **each slice**,
which is what this migration creates. A declared dependency between sources needs no table: when
the ingestion becomes a pipeline, a dependency is an edge of the graph (`fonte` with
`origem:"pipeline"`), and `pipelines/dag.py` already resolves the order and runs the upstream first.
Creating an `ingestao_dependencias` next to that would be two mechanisms saying the same thing.

**Why a new table and not `ingestao_lotes`.** A batch is what LANDED: it only exists when the
origin returned bytes that became an object in the lake. A slice the origin did not publish (404 on
a weekend), one that returned `304 Not Modified` and one that failed during discovery **generate no
batch at all** - and those are exactly the ones someone opens the screen to understand. Today they
vanish: `totais()` counts `nao_publicados`/`nao_modificados`, and `finalizar_execucao` stores only
four of the six numbers. An execution of 31 slices that brought 22 and found 9 weekends shows up as
"22 batches" without saying what happened to the other 9 - and there is no way to tell that apart
from 9 lost slices.

**The uniqueness is `(execucao_id, chave_lote)`, and `tentativa` is the reason.** The Temporal
activity retries up to 3x per slice (`POLITICA` in `ingestao_workflow.py`); without the unique key,
a slice that failed twice and passed on the third would become three rows and the screen would
count 3 slices where there is 1. With it, `INSERT ... ON CONFLICT DO UPDATE` overwrites the outcome
and **increments** `tentativa` - precisely the signal a workflow scheduler shows and that is
otherwise lost inside Temporal.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE ingestao_fatias (
      id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      execucao_id   uuid NOT NULL REFERENCES ingestao_execucoes(id) ON DELETE CASCADE,
      fonte_id      uuid NOT NULL REFERENCES ingestao_fontes(id) ON DELETE CASCADE,
      chave_lote    text NOT NULL,
      janela_inicio date,
      janela_fim    date,
      -- SUMMARIZED outcome of the slice. A slice may generate several requests (a multi-file zip,
      -- paginated OData), so `contagens` keeps the detail and `desfecho` keeps what needs
      -- attention: falha > quarentena > novo > reusado > nao_modificado > nao_publicado > vazio.
      desfecho      text NOT NULL,
      contagens     jsonb NOT NULL DEFAULT '{}'::jsonb,
      -- How many times this slice ran WITHIN this execution (orchestrator retry).
      tentativa     int NOT NULL DEFAULT 1,
      duracao_ms    int,
      -- Only for `falha`/`quarentena`: human-readable message, no secrets and no stack (same rule as
      -- the error envelope). The raw payload stays in quarantine; here is the readable reason.
      erro          text,
      iniciada_em   timestamptz NOT NULL DEFAULT now(),
      concluida_em  timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_ingestao_fatia UNIQUE (execucao_id, chave_lote),
      CONSTRAINT ingestao_fatias_desfecho_chk CHECK (desfecho IN
        ('novo','reusado','nao_modificado','nao_publicado','quarentena','falha','vazio'))
    )
    """,
    # The log of ONE execution, in slice order (this is how the screen reads it).
    "CREATE INDEX idx_ingestao_fatias_execucao ON ingestao_fatias (execucao_id, chave_lote)",
    # "what happened to slice 2026-08-03 on this source, over time" - the history question.
    "CREATE INDEX idx_ingestao_fatias_fonte ON ingestao_fatias "
    "(fonte_id, chave_lote, concluida_em DESC)",
    # --- The two outcomes that `totais()` computes and the execution did not store. ---
    # Without them, "22 batches from a 31-day window" cannot tell "9 weekends" from "9 lost".
    "ALTER TABLE ingestao_execucoes ADD COLUMN lotes_nao_publicados int NOT NULL DEFAULT 0",
    "ALTER TABLE ingestao_execucoes ADD COLUMN lotes_nao_modificados int NOT NULL DEFAULT 0",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ingestao_fatias TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ingestao_fatias")
    op.execute("ALTER TABLE ingestao_execucoes DROP COLUMN IF EXISTS lotes_nao_modificados")
    op.execute("ALTER TABLE ingestao_execucoes DROP COLUMN IF EXISTS lotes_nao_publicados")
