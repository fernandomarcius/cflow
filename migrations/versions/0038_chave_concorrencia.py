"""Duplicate execution: `execucoes.chave_concorrencia` + partial unique index over those in flight.

**What a duplicate is.** The same pipeline triggered with the SAME parameters while the previous
one is still `executando`. Measured on production-like hosts before deciding: hundreds of
overlapping pairs from a scheduler accumulating on every tick, double clicks on a refresh button
within minutes, and scripts re-fired seconds later. Every duplicate REPEATS the effect nodes
(e-mail, POST, `append`) and competes for the lake's `_atual.json` - with nobody knowing there were
two.

**Where the key is computed.** In SQL, always by the SAME expression:
`md5(pipeline_id::text || parametros::text)` over the already normalized and redacted parameters
(`jsonb::text` is canonical in Postgres - key order and whitespace do not change the key; it is the
same reason `execucoes_do_dia` compares `e.parametros = CAST(:params AS jsonb)`). A redacted secret
(`***`) enters the same for any value: two triggers that differ only in the credential count as
equal.

**Only the trigger's ROOT carries a key.** An execution that is a dependency inside a chain
(`cadeia_id` set and `cadeia_raiz=false`) stays `NULL` - and `NULL` never collides in the index.
Two chains of the same project needing the same upstream pipeline with identical parameters happen
every week; blocking the upstream would abort the whole chain over a duplicate that today only
costs recomputation.

**The partial unique index is the real protection.** The pre-check in `criar_execucao` (a `SELECT`
before the `INSERT`) handles the common case; two concurrent `INSERT`s would both pass the `SELECT`
- the index decides, and the `IntegrityError` becomes the same 409. A tiny index: only the rows in
flight.

**Before migrating in production:** if a duplicate is ALREADY in flight, the `CREATE UNIQUE INDEX`
fails - which is the right behavior. Reconcile the executions first (close what died); whatever is
genuinely still in flight has to finish.

Revision ID: 0038
Revises: 0036
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision = "0038"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE execucoes ADD COLUMN chave_concorrencia text")
    # Only the in-flight ones that are roots (no chain, or root of one) - dependencies stay NULL.
    op.execute(
        "UPDATE execucoes SET chave_concorrencia = md5(pipeline_id::text || parametros::text) "
        "WHERE status = 'executando' AND (cadeia_id IS NULL OR cadeia_raiz)"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_execucoes_em_voo ON execucoes (chave_concorrencia) "
        "WHERE status = 'executando'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_execucoes_em_voo")
    op.execute("ALTER TABLE execucoes DROP COLUMN IF EXISTS chave_concorrencia")
