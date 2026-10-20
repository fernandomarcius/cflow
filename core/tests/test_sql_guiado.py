"""Guided operations -> SQL: keyed join (default) and UNION mode (stacking 2+ inputs)."""

from __future__ import annotations

import duckdb
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.pipelines.sql_guiado import gerar_sql, validar_uniao


def test_juntar_default_continua_join():
    sql = gerar_sql({"tipo": "juntar", "condicao": "n1.id = n2.id"}, entradas=["n1", "n2"])
    assert "JOIN" in sql.upper()
    assert "UNION" not in sql.upper()


def test_juntar_exige_duas_entradas():
    with pytest.raises(ErroAplicacao) as exc:
        gerar_sql({"tipo": "juntar", "condicao": "a = b"}, entradas=["n1"])
    assert exc.value.codigo == "op_invalida"


def test_uniao_empilha_por_nome():
    sql = gerar_sql({"tipo": "juntar", "modo": "uniao"}, entradas=["n1", "n2"])
    assert "UNION ALL BY NAME" in sql.upper()


def test_uniao_distinta_remove_duplicatas():
    sql = gerar_sql({"tipo": "juntar", "modo": "uniao", "distinto": True}, entradas=["n1", "n2"])
    assert "UNION BY NAME" in sql.upper()
    assert "UNION ALL" not in sql.upper()


def test_uniao_aceita_tres_ou_mais_entradas():
    sql = gerar_sql({"tipo": "juntar", "modo": "uniao"}, entradas=["n1", "n2", "n3"])
    assert sql.upper().count("UNION ALL BY NAME") == 2
    duck = duckdb.connect()
    for i, n in enumerate(["n1", "n2", "n3"]):
        duck.execute(f"CREATE TABLE {n} AS SELECT {i} AS a, {i * 10} AS b")
    assert duck.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0] == 3


def test_sem_entrada_erro():
    with pytest.raises(ErroAplicacao) as exc:
        gerar_sql({"tipo": "filtrar", "condicao": "a > 0"}, entradas=[])
    assert exc.value.codigo == "op_sem_entrada"


# --- validar_uniao (pre-check with a clear error before DuckDB) ---


def _duck_com(tabelas: dict[str, str]):
    duck = duckdb.connect()
    for nome, projecao in tabelas.items():
        duck.execute(f'CREATE TABLE "{nome}" AS SELECT {projecao}')
    return duck


def test_validar_uniao_mesmas_colunas_passa():
    duck = _duck_com({"n1": "1 AS a, 2 AS b", "n2": "20 AS b, 10 AS a"})
    validar_uniao(duck, {"tipo": "juntar", "modo": "uniao"}, ["n1", "n2"])  # does not raise


def test_validar_uniao_colunas_diferentes_erro_claro():
    duck = _duck_com({"n1": "1 AS a, 2 AS b", "n2": "1 AS a, 3 AS c"})
    with pytest.raises(ErroAplicacao) as exc:
        validar_uniao(duck, {"tipo": "juntar", "modo": "uniao"}, ["n1", "n2"])
    assert exc.value.codigo == "uniao_colunas_diferentes"
    assert "'n1' is missing: c" in exc.value.mensagem
    assert "'n2' is missing: b" in exc.value.mensagem


def test_validar_uniao_ignora_join_e_outras_ops():
    duck = duckdb.connect()  # no tables at all: if it queried anything, it would fail
    validar_uniao(duck, {"tipo": "juntar", "condicao": "a = b"}, ["n1", "n2"])
    validar_uniao(duck, {"tipo": "filtrar", "condicao": "a > 0"}, ["n1"])
