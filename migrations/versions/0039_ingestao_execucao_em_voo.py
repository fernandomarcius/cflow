"""Ingestion: one in-flight execution per source - partial unique index on `ingestao_execucoes`.

Same pattern as 0038 (`execucoes.chave_concorrencia`), applied to the external source: two
executions of the SAME source at the same time (scheduler + manual trigger, backfill +
incremental) read the same watermark, resolve the same window and download the same slices twice -
the batch is deduplicated by the `(fonte, chave_lote, sha256)` index, but network traffic against
the public origin is 2x, each execution has ITS OWN concurrency semaphore (2x the ceiling declared
in the contract), and the second may ADVANCE the watermark over a slice the first one quarantined
(the second sees everything as `reusado`, the first refused to advance). Here the key is only
`fonte_id`: a source has no parameters.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX ux_ingestao_execucoes_em_voo ON ingestao_execucoes (fonte_id) "
        "WHERE status = 'executando'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_ingestao_execucoes_em_voo")
