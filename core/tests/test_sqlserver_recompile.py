"""`OPTION (RECOMPILE)` on the EXECUTION read of SQL Server (`sqlserver._com_recompile`).

It exists because SQL Server reuses the plan compiled for the first parameter value that used the
text, and a nightly load pushes many operations through the same text; a bad cached plan can triple
the logical reads and blow past the activity timeout. Preview and slices do NOT go through here:
the preview wraps the SQL in a subquery (where `OPTION` is invalid) and the slice already appends
its own.
"""

from __future__ import annotations

import pytest

from cflow_core.conexoes.conectores import sqlserver

R = "\nOPTION (RECOMPILE)"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t WHERE x = %s",
        "  select a from t",
        "WITH c AS (SELECT 1 AS a) SELECT a FROM c",
        "-- comment at the top\nSELECT a FROM t",
        "/* bloco */ SELECT a FROM t",
    ],
)
def test_select_ou_cte_ganha_o_hint_numa_linha_nova(sql):
    assert sqlserver._com_recompile(sql) == sql.rstrip() + R


def test_ponto_e_virgula_final_sai_antes_do_hint():
    assert sqlserver._com_recompile("SELECT a FROM t;  \n") == "SELECT a FROM t" + R


def test_comentario_de_linha_no_fim_nao_engole_o_hint():
    sql = "SELECT a FROM t -- author note"
    out = sqlserver._com_recompile(sql)
    assert out == sql + R
    assert out.splitlines()[-1] == "OPTION (RECOMPILE)"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t OPTION (MAXDOP 1)",  # whoever wrote their own OPTION decides
        "select a from t option(recompile)",
        "EXEC dbo.sp_x %s",  # a hint at the end of EXEC is invalid
        "SET NOCOUNT ON; SELECT a FROM t",  # batch: the hint would land on the wrong statement
        "SELECT a FROM t; -- fim",  # a hint after the `;` would become a stray statement
        "UPDATE t SET a = 1",
        "",
    ],
)
def test_fora_do_caso_seguro_o_sql_passa_intacto(sql):
    assert sqlserver._com_recompile(sql) == sql


def test_ponto_e_virgula_e_option_dentro_de_literal_nao_contam():
    sql = "SELECT a FROM t WHERE b = 'x; OPTION (y)'"
    assert sqlserver._com_recompile(sql) == sql + R


class _Capturar:
    def __init__(self):
        self.query = None

    def amostra(self, config, senha, query, *resto):
        self.query = query
        return [], []

    def extrair(self, config, senha, query, *resto):
        self.query = query
        return "extracao"


@pytest.fixture
def capturar(monkeypatch):
    c = _Capturar()
    monkeypatch.setattr(sqlserver, "_amostra_serializada", c.amostra)
    monkeypatch.setattr(sqlserver, "_extrair_tabela_sync", c.extrair)
    return c


async def test_execucao_pela_amostra_recompila(capturar):
    await sqlserver.ConectorSqlServer().amostra({}, None, "SELECT a FROM t", None)
    assert capturar.query == "SELECT a FROM t" + R


async def test_previa_nao_recompila_e_continua_embrulhada(capturar):
    await sqlserver.ConectorSqlServer().amostra({}, None, "SELECT a FROM t", 50)
    assert capturar.query == "SELECT TOP 50 * FROM (SELECT a FROM t) AS _sub"


async def test_extracao_colunar_recompila(capturar):
    await sqlserver.ConectorSqlServer().extrair_tabela({}, None, "SELECT a FROM t")
    assert capturar.query == "SELECT a FROM t" + R
