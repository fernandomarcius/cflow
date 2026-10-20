"""Preventive index maintenance: removes exact duplicates, indexes the FKs of tables that grow
per execution, and gives the audit trail an index that matches the query by period.

Motivation (measured on a production-like host before raising the cadence to several
executions per day):

1. **Six exact duplicate indexes.** Each pair is (UNIQUE constraint) + (plain index) over the SAME
   columns: `sessoes(token_hash)`, `pipeline_versoes(pipeline_id, numero)` and the equivalents for
   cube/view/page/library. The planner only uses one; the other is duplicated write work on every
   INSERT/UPDATE (heap + index + WAL) and one more structure for autovacuum to clean. The `idx_*`
   goes away and the constraint stays - it cannot be dropped, it is what guarantees uniqueness.

2. **FKs without an index.** A `DELETE` on the parent scans the whole child to check the reference,
   and the `version -> author` JOIN has no way in. We only index the FKs whose child GROWS with
   usage (session, execution, version, report). Deliberately left out are the FKs of human-curated
   catalogs (`pipelines.dono_id`, `conexoes.criado_por`, `agendamentos.*`...): they are dozens of
   rows, a seq scan is cheaper than maintaining the index, and an index on a small hot table only
   adds writes.

3. **Audit trail without a time index.** `auditoria` has (ator_id, ocorrido_em) and (recurso_tipo,
   recurso_id), but the trail query filters by period and orders by `ocorrido_em, id`
   (`auditoria/consulta.py`) - without an actor filter that is a seq scan + sort. The
   (ocorrido_em, id) index matches the exact ORDER BY, so pagination becomes an index scan
   without a sort.

4. **Autovacuum never fires on small, hot tables.** The default (scale_factor 0.2 + threshold 50)
   needs ~60 dead tuples on a 3-row table: `agendamentos` had 10 dead for 3 live, `paginas` 32 for
   7, `pipelines` 30 for 7 - the scheduler poller issues an UPDATE every cycle. It does not hurt at
   small sizes, but it is bloat that only grows and is never reclaimed. We lower the per-table
   trigger on the tables that suffer frequent UPDATEs.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


# (duplicate index, UNIQUE constraint that covers the same columns and stays)
DUPLICADOS = [
    ("idx_sessoes_token", "sessoes_token_hash_key"),
    ("idx_pipe_versoes", "pipeline_versoes_pipeline_id_numero_key"),
    ("idx_cubo_versoes", "cubo_versoes_cubo_id_numero_key"),
    ("idx_view_versoes", "view_versoes_view_id_numero_key"),
    ("idx_pagina_versoes", "pagina_versoes_pagina_id_numero_key"),
    ("idx_biblioteca_funcoes_versoes", "biblioteca_funcoes_versoes_biblioteca_id_numero_key"),
]

# Recreation used only in the downgrade (the original definition of each duplicate).
RECRIAR = {
    "idx_sessoes_token": "CREATE INDEX idx_sessoes_token ON sessoes (token_hash)",
    "idx_pipe_versoes": "CREATE INDEX idx_pipe_versoes ON pipeline_versoes (pipeline_id, numero)",
    "idx_cubo_versoes": "CREATE INDEX idx_cubo_versoes ON cubo_versoes (cubo_id, numero)",
    "idx_view_versoes": "CREATE INDEX idx_view_versoes ON view_versoes (view_id, numero)",
    "idx_pagina_versoes": "CREATE INDEX idx_pagina_versoes ON pagina_versoes (pagina_id, numero)",
    "idx_biblioteca_funcoes_versoes": (
        "CREATE INDEX idx_biblioteca_funcoes_versoes "
        "ON biblioteca_funcoes_versoes (biblioteca_id, numero)"
    ),
}

# FKs of tables that grow with usage. Short, stable name; `IF NOT EXISTS` because the migration
# may reach a database where someone already created the index by hand during maintenance.
INDICES_FK = [
    ("idx_sessoes_usuario", "sessoes (usuario_id)"),
    ("idx_exec_iniciada_por", "execucoes (iniciada_por)"),
    ("idx_exec_agendamento", "execucoes (agendamento_id)"),
    ("idx_pipe_versoes_autor", "pipeline_versoes (autor_id)"),
    ("idx_pagina_versoes_autor", "pagina_versoes (autor_id)"),
    ("idx_cubo_versoes_autor", "cubo_versoes (autor_id)"),
    ("idx_view_versoes_autor", "view_versoes (autor_id)"),
    ("idx_biblioteca_versoes_autor", "biblioteca_funcoes_versoes (autor_id)"),
    ("idx_relatorios_gerado_por", "relatorios (gerado_por)"),
    ("idx_usuarios_grupos_grupo", "usuarios_grupos (grupo_id)"),
]

# Tables with frequent UPDATEs and few rows: the default autovacuum trigger is never reached and
# dead tuples pile up. Low threshold + small scale_factor = regular cleanup.
AUTOVACUUM_AGRESSIVO = [
    "agendamentos",
    "pipelines",
    "paginas",
    "views",
    "cubos",
    "biblioteca_funcoes",
    "conexoes",
    "usuarios",
    "execucoes",
    "sessoes",
]

# Tables where the SAME row is rewritten all the time: `sessoes.ultima_atividade_em` changes on
# every authenticated request and `agendamentos.proxima_em`/`ultimo_disparo_em` on every poller
# cycle. With a full page Postgres cannot do a HOT update - it writes the new version to another
# page and has to update ALL the table's indexes. Leaving 30% free keeps the new version on the
# same page (none of the updated columns is indexed), and the space is reused after vacuum.
FILLFACTOR_HOT = ["sessoes", "agendamentos"]


def upgrade() -> None:
    # Without `pg_stat_statements` there is no answer to "which query is slow" - only guesses. The
    # extension requires `shared_preload_libraries` (set in the docker-compose `command:`) and only
    # becomes available after Postgres restarts; on a database that has not restarted yet the CREATE
    # fails, and failing the whole migration over observability would trade one problem for a worse one.
    op.execute(
        "DO $$ BEGIN "
        "  CREATE EXTENSION IF NOT EXISTS pg_stat_statements; "
        "EXCEPTION WHEN OTHERS THEN "
        "  RAISE NOTICE 'pg_stat_statements unavailable: restart Postgres with "
        "shared_preload_libraries and run CREATE EXTENSION'; "
        "END $$"
    )

    for indice, _constraint in DUPLICADOS:
        op.execute(f"DROP INDEX IF EXISTS {indice}")

    for nome, alvo in INDICES_FK:
        op.execute(f"CREATE INDEX IF NOT EXISTS {nome} ON {alvo}")

    # Trail by period (`ocorrido_em, id` = the exact ORDER BY of the paginated query).
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_ocorrido ON auditoria (ocorrido_em, id)")
    # Purge of expired sessions (nightly job) - DELETE ... WHERE expira_em < now().
    op.execute("CREATE INDEX IF NOT EXISTS idx_sessoes_expira ON sessoes (expira_em)")
    # Purge of old login attempts; the existing index starts with email_tentado.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tentativas_ocorrido ON tentativas_login (ocorrido_em)"
    )

    for tabela in AUTOVACUUM_AGRESSIVO:
        op.execute(
            f"ALTER TABLE {tabela} SET ("
            "autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_threshold = 50, "
            "autovacuum_analyze_scale_factor = 0.02, autovacuum_analyze_threshold = 50)"
        )

    for tabela in FILLFACTOR_HOT:
        op.execute(f"ALTER TABLE {tabela} SET (fillfactor = 70)")


def downgrade() -> None:
    for tabela in FILLFACTOR_HOT:
        op.execute(f"ALTER TABLE {tabela} RESET (fillfactor)")

    for tabela in AUTOVACUUM_AGRESSIVO:
        op.execute(
            f"ALTER TABLE {tabela} RESET ("
            "autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold, "
            "autovacuum_analyze_scale_factor, autovacuum_analyze_threshold)"
        )

    op.execute("DROP INDEX IF EXISTS idx_tentativas_ocorrido")
    op.execute("DROP INDEX IF EXISTS idx_sessoes_expira")
    op.execute("DROP INDEX IF EXISTS idx_audit_ocorrido")

    for nome, _alvo in INDICES_FK:
        op.execute(f"DROP INDEX IF EXISTS {nome}")

    for indice, _constraint in DUPLICADOS:
        op.execute(RECRIAR[indice])
