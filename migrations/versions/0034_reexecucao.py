"""Re-execution: its own origin, link to the original execution and a mark for the chain root.

**`origem='reexecucao'`** joins the CHECK (0018 only accepted `manual|agendado`). The same
principle as the ingestion retry applies: re-executing NEVER reopens the previous execution - it
creates a new one. Rewriting yesterday's outcome would erase the evidence that there was a failure,
and that evidence is exactly what one came to investigate. With its own origin, the history says
what each row is.

**`reexecucao_de` / `reexecucao_desde_no`** link the new execution to the original and record the
point it restarted from. Without them, a re-execution would be a loose execution and nobody would
know it came from another - neither the screen, nor the trail, nor whoever reconciles months later.

**`cadeia_raiz`** marks, within a `cadeia_id`, WHICH execution was the one requested. Migration 0020
gave a FLAT UUID: it says "these N executions came from the same click", but not which of them is
the root and which are dependencies that ran by table. Without this mark, the listing cannot pick
the group header - and picking wrong would invert the reading (the dependency would appear calling
whoever called it). `_executar_dag` already knows the root at runtime; the column only persists
what it knows.

An execution WITHOUT a chain (`cadeia_id IS NULL`) is its own root and does not depend on the mark
- so the `false` default does not lie: it only describes executions that belong to a chain.

The `execucoes` grant (0008) is per table: a new column comes for free.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-01
"""

from __future__ import annotations

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

DDL = [
    "ALTER TABLE execucoes DROP CONSTRAINT IF EXISTS execucoes_origem_chk",
    "ALTER TABLE execucoes ADD CONSTRAINT execucoes_origem_chk "
    "CHECK (origem IN ('manual', 'agendado', 'reexecucao'))",
    "ALTER TABLE execucoes ADD COLUMN reexecucao_de uuid REFERENCES execucoes(id)",
    "ALTER TABLE execucoes ADD COLUMN reexecucao_desde_no text",
    "ALTER TABLE execucoes ADD COLUMN cadeia_raiz boolean NOT NULL DEFAULT false",
    # Partial index: only re-executions matter for the question "what came from this execution?".
    "CREATE INDEX idx_exec_reexec ON execucoes (reexecucao_de) WHERE reexecucao_de IS NOT NULL",
]


def upgrade() -> None:
    for c in DDL:
        op.execute(c)


def downgrade() -> None:
    for c in [
        "DROP INDEX IF EXISTS idx_exec_reexec",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS cadeia_raiz",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS reexecucao_desde_no",
        # Executions with origin 'reexecucao' would violate the old CHECK again; the downgrade
        # normalizes them to 'manual' BEFORE tightening the rule, otherwise the migration fails midway.
        "UPDATE execucoes SET origem = 'manual' WHERE origem = 'reexecucao'",
        "ALTER TABLE execucoes DROP COLUMN IF EXISTS reexecucao_de",
        "ALTER TABLE execucoes DROP CONSTRAINT IF EXISTS execucoes_origem_chk",
        "ALTER TABLE execucoes ADD CONSTRAINT execucoes_origem_chk "
        "CHECK (origem IN ('manual', 'agendado'))",
    ]:
        op.execute(c)
