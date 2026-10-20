"""GROUPED report: ONE xlsx with the sheets of several sources.

Until now the export was the tuple `(execucao_id, no_id, layout_id, layout_versao, formato)` - a
single source. A "grouped" button asks for ONE file with the sheets of several sources, and the
facade used to stitch ready-made xlsx files together cell by cell (hundreds of MB, hundreds of
thousands of rows, minutes). Now cFlow generates the whole file, during the run, from the snapshot.

**`relatorio_exportacoes.tipo`** in `simples` | `agrupado`. A grouped export has no single
execution nor a single node: `execucao_id` and `no_id` are null (the CHECK guarantees only it can
be) and the identity is the set of parts. `cadeia_id` is the run when all the parts come from the
same one - informative; the key does not depend on it.

**`relatorio_exportacao_partes`** - one row per sheet, in file order:
`(aba, execucao_id, no_id, layout_id, layout_versao)`. The grouped `chave` is the hash of ALL the
parts + grouped layout + version + format; the partial unique index from 0036 still holds for both
types - same request, one record, one file. No UPDATE or DELETE for `cflow_app`: the parts are the
provenance of the artifact, written in the SAME transaction as the record and the trail.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE relatorio_exportacoes ADD COLUMN tipo text NOT NULL DEFAULT 'simples' "
    "CHECK (tipo IN ('simples', 'agrupado'))",
    "ALTER TABLE relatorio_exportacoes ADD COLUMN cadeia_id uuid",
    "ALTER TABLE relatorio_exportacoes ALTER COLUMN execucao_id DROP NOT NULL",
    "ALTER TABLE relatorio_exportacoes ALTER COLUMN no_id DROP NOT NULL",
    # Only the grouped export may omit execution and node: a simple export is still the usual tuple.
    "ALTER TABLE relatorio_exportacoes ADD CONSTRAINT ck_relatorio_exportacoes_tipo_fonte "
    "CHECK (tipo = 'agrupado' OR (execucao_id IS NOT NULL AND no_id IS NOT NULL))",
    """
    CREATE TABLE relatorio_exportacao_partes (
      exportacao_id  uuid NOT NULL REFERENCES relatorio_exportacoes(id),
      ordem          int NOT NULL CHECK (ordem >= 1),
      aba            text NOT NULL,
      execucao_id    uuid NOT NULL REFERENCES execucoes(id),
      no_id          text NOT NULL,
      layout_id      uuid NOT NULL REFERENCES relatorio_layouts(id),
      layout_versao  int NOT NULL,
      PRIMARY KEY (exportacao_id, ordem),
      UNIQUE (exportacao_id, aba)
    )
    """,
    "CREATE INDEX idx_relatorio_exportacao_partes_exec ON relatorio_exportacao_partes "
    "(execucao_id)",
    "CREATE INDEX idx_relatorio_exportacao_partes_layout ON relatorio_exportacao_partes "
    "(layout_id)",
    "GRANT SELECT, INSERT ON relatorio_exportacao_partes TO cflow_app",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP TABLE IF EXISTS relatorio_exportacao_partes",
        "DELETE FROM relatorio_exportacoes WHERE tipo = 'agrupado'",
        "ALTER TABLE relatorio_exportacoes DROP CONSTRAINT IF EXISTS "
        "ck_relatorio_exportacoes_tipo_fonte",
        "ALTER TABLE relatorio_exportacoes ALTER COLUMN no_id SET NOT NULL",
        "ALTER TABLE relatorio_exportacoes ALTER COLUMN execucao_id SET NOT NULL",
        "ALTER TABLE relatorio_exportacoes DROP COLUMN IF EXISTS cadeia_id",
        "ALTER TABLE relatorio_exportacoes DROP COLUMN IF EXISTS tipo",
    ]:
        op.execute(c)
