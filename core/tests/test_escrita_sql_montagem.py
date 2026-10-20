"""Golden of the generated write SQL (per strategy x driver) + montar_plano. Pure, no database.

Invariant: the SQL built here is EXACTLY what runs at execution (connectors) and what the preview
shows (montar_plano.sql_gerado) -- same builder in both paths.
"""

from __future__ import annotations

import pytest

from cflow_core.conexoes.conectores.postgres import montar_sql_tabela_pg
from cflow_core.conexoes.conectores.sqlserver import montar_sql_tabela_mssql
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao.no_escrita_sql import montar_plano

# ---------------- Postgres ----------------


def test_pg_append():
    sql = montar_sql_tabela_pg("public", "alvo", ["issuer_code", "valor"], "append", None)
    assert sql == (
        'INSERT INTO "public"."alvo" ("issuer_code", "valor") '
        'SELECT "issuer_code", "valor" FROM _stage'
    )


def test_pg_truncate_insert_igual_append():
    # truncate_insert = TRUNCATE (separate) + the SAME INSERT ... SELECT as append.
    a = montar_sql_tabela_pg("s", "t", ["a"], "append", None)
    ti = montar_sql_tabela_pg("s", "t", ["a"], "truncate_insert", None)
    assert a == ti


def test_pg_upsert():
    sql = montar_sql_tabela_pg("public", "alvo", ["id", "valor"], "upsert", ["id"])
    assert sql == (
        'INSERT INTO "public"."alvo" ("id", "valor") SELECT "id", "valor" FROM _stage '
        'ON CONFLICT ("id") DO UPDATE SET "valor" = EXCLUDED."valor"'
    )


def test_pg_upsert_todas_colunas_chave_do_nothing():
    sql = montar_sql_tabela_pg("s", "t", ["id"], "upsert", ["id"])
    assert sql.endswith('ON CONFLICT ("id") DO NOTHING')


def test_pg_estrategia_invalida():
    with pytest.raises(ErroAplicacao) as exc:
        montar_sql_tabela_pg("s", "t", ["a"], "merge", None)
    assert exc.value.codigo == "escrita_estrategia"


def test_pg_cast_tipos_na_execucao():
    # Execution: staging is TEXT; the text->target-type cast goes into the SELECT (asyncpg does not
    # convert str->int on bind). Without `tipos` (preview) there is no cast -- the test above
    # covers that case.
    sql = montar_sql_tabela_pg(
        "billing",
        "open_items",
        ["base_code", "project"],
        "append",
        None,
        {"base_code": "smallint", "project": "character varying(20)"},
    )
    assert sql == (
        'INSERT INTO "billing"."open_items" ("base_code", "project") '
        'SELECT "base_code"::smallint, "project"::character varying(20) FROM _stage'
    )


def test_pg_cast_tipos_parcial_sem_tipo_fica_sem_cast():
    # Column missing from the type map -> no cast (defensive; should not happen).
    sql = montar_sql_tabela_pg("s", "t", ["a", "b"], "append", None, {"a": "integer"})
    assert 'SELECT "a"::integer, "b" FROM _stage' in sql


# ---------------- SQL Server ----------------


def test_mssql_append():
    sql = montar_sql_tabela_mssql("dbo", "alvo", ["id", "valor"], "append", None)
    assert sql == "INSERT INTO [dbo].[alvo] ([id], [valor]) SELECT [id], [valor] FROM #stage"


def test_mssql_upsert_merge():
    sql = montar_sql_tabela_mssql("dbo", "alvo", ["id", "valor"], "upsert", ["id"])
    assert sql == (
        "MERGE [dbo].[alvo] AS a USING #stage AS s ON (a.[id] = s.[id]) "
        "WHEN MATCHED THEN UPDATE SET a.[valor] = s.[valor] "
        "WHEN NOT MATCHED THEN INSERT ([id], [valor]) VALUES (s.[id], s.[valor]);"
    )
    assert sql.endswith(";")  # MERGE requires a terminator


def test_mssql_upsert_todas_chave_sem_matched():
    sql = montar_sql_tabela_mssql("dbo", "alvo", ["id"], "upsert", ["id"])
    assert "WHEN MATCHED" not in sql
    assert "WHEN NOT MATCHED THEN INSERT ([id]) VALUES (s.[id]);" in sql


# ---------------- montar_plano ----------------


def test_plano_tabela_homonimas():
    cfg = {
        "conexao_id": "c",
        "modo": "tabela",
        "schema": "public",
        "tabela": "alvo",
        "estrategia": "append",
    }
    p = montar_plano(cfg, ["a", "b"], "postgres")
    assert p.modo == "tabela"
    assert p.destino == "public.alvo"
    assert p.colunas_destino == ["a", "b"]
    assert p.projecao == [("a", "a"), ("b", "b")]
    assert p.sql_gerado == montar_sql_tabela_pg("public", "alvo", ["a", "b"], "append", None)


def test_plano_tabela_mapeamento():
    cfg = {
        "modo": "tabela",
        "schema": "s",
        "tabela": "t",
        "estrategia": "upsert",
        "chave": ["code"],
        "mapeamento": [{"de": "issuer", "para": "code"}, {"de": "v", "para": "valor"}],
    }
    p = montar_plano(cfg, ["issuer", "v", "ignored"], "postgres")
    assert p.projecao == [("issuer", "code"), ("v", "valor")]
    assert p.colunas_destino == ["code", "valor"]
    assert p.chave == ["code"]


def test_plano_mapeamento_de_ausente_falha():
    # SOURCE column of the mapping that does not exist in the input -> fail early (else it would write NULL).
    cfg = {
        "modo": "tabela",
        "schema": "s",
        "tabela": "t",
        "estrategia": "append",
        "mapeamento": [{"de": "issuer", "para": "code"}, {"de": "inexistente", "para": "x"}],
    }
    with pytest.raises(ErroAplicacao) as exc:
        montar_plano(cfg, ["issuer", "v"], "postgres")
    assert exc.value.codigo == "escrita_coluna_ausente"
    assert "inexistente" in exc.value.mensagem


def test_plano_tabela_dialeto_mssql():
    cfg = {"modo": "tabela", "schema": "dbo", "tabela": "t", "estrategia": "append"}
    p = montar_plano(cfg, ["a"], "sqlserver")
    assert "#stage" in p.sql_gerado and "[dbo].[t]" in p.sql_gerado


def test_plano_sql_detecta_entrada():
    cfg = {"modo": "sql", "sql": "INSERT INTO alvo SELECT CAST(id AS int) FROM _entrada"}
    p = montar_plano(cfg, ["id"], "postgres")
    assert p.modo == "sql" and p.usa_entrada is True
    assert p.sql_gerado == cfg["sql"]


def test_plano_sql_sem_entrada():
    cfg = {"modo": "sql", "sql": "DELETE FROM alvo WHERE ativo = false"}
    p = montar_plano(cfg, ["id"], "postgres")
    assert p.usa_entrada is False
