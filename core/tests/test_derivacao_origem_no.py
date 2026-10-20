"""Derivation resolution with `no` origin -- materializes the output of a node of the SAME pipeline
as a map. Pure SQL nodes do not touch the database (in-memory DuckDB), so this runs without Postgres."""

from __future__ import annotations

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.pipelines.derivacao import (
    _materializar_no_mapa,
    opcoes_de_no,
    resolver_derivados,
)


def _def_mapa_sql():
    """Pipeline with a `mapa` node (pure SQL) that emits (id, system) pairs -- the derivation map."""
    return {
        "nos": [
            {
                "id": "mapa",
                "tipo": "sql",
                "sql": (
                    "SELECT * FROM (VALUES (43, 'ALPHA'), (44, 'BETA'), (43, 'ALPHA')) "
                    "AS t(UnitId, SystemName)"
                ),
            }
        ],
        "arestas": [],
        "parametros": [
            {"nome": "operacao_id", "tipo": "inteiro"},
            {
                "nome": "sistema",
                "tipo": "texto",
                "derivacao": {
                    "origem": "no",
                    "de": "operacao_id",
                    "no_id": "mapa",
                    "campo_id": "UnitId",
                    "campo_valor": "SystemName",
                },
            },
        ],
    }


async def test_materializar_no_mapa_retorna_linhas():
    linhas = await _materializar_no_mapa(_def_mapa_sql(), "mapa", {})
    # 3 raw rows (the DISTINCT is done in _resolver_um, not here).
    assert len(linhas) == 3
    assert {str(r["UnitId"]) for r in linhas} == {"43", "44"}


async def test_resolver_derivados_origem_no():
    definicao = _def_mapa_sql()
    out = await resolver_derivados(definicao, {"operacao_id": "43"})
    assert out["sistema"] == "ALPHA"


async def test_resolver_derivados_origem_no_sem_match_fica_ausente():
    definicao = _def_mapa_sql()
    out = await resolver_derivados(definicao, {"operacao_id": "999"})
    assert "sistema" not in out  # no match -> derivation does not fill it in


async def test_materializar_no_mapa_rejeita_no_de_efeito():
    definicao = {
        "nos": [
            {"id": "fonte", "tipo": "sql", "sql": "SELECT 1 AS x"},
            {"id": "grava", "tipo": "escrita_sql", "config": {"conexao_id": "x", "modo": "tabela"}},
        ],
        "arestas": [{"de": "fonte", "para": "grava"}],
    }
    with pytest.raises(ErroAplicacao) as ei:
        await _materializar_no_mapa(definicao, "grava", {})
    assert ei.value.codigo == "derivacao_no_invalido"


async def test_materializar_no_mapa_no_inexistente():
    with pytest.raises(ErroAplicacao) as ei:
        await _materializar_no_mapa(_def_mapa_sql(), "fantasma", {})
    assert ei.value.codigo == "derivacao_no_inexistente"


# ---- opcoes_de_no: dropdown options from a node's output ----


def _def_opcoes_sql():
    """`lista` node (pure SQL) with (id, nickname) pairs to populate an operator dropdown."""
    return {
        "nos": [
            {
                "id": "lista",
                "tipo": "sql",
                "sql": (
                    "SELECT * FROM (VALUES (2, 'Beta'), (10, 'Dez'), (1, 'Alfa'), (2, 'Beta')) "
                    "AS t(opid, apelido)"
                ),
            }
        ],
        "arestas": [],
    }


async def test_opcoes_de_no_distintos_e_rotulo():
    out = await opcoes_de_no(_def_opcoes_sql(), "lista", "opid", "apelido")
    # DISTINCT (2 appears twice -> once) and NUMERIC order by value (1,2,10, not 1,10,2).
    assert out == [
        {"valor": "1", "rotulo": "Alfa"},
        {"valor": "2", "rotulo": "Beta"},
        {"valor": "10", "rotulo": "Dez"},
    ]


async def test_opcoes_de_no_sem_rotulo_usa_valor():
    out = await opcoes_de_no(_def_opcoes_sql(), "lista", "opid")
    assert {o["valor"] for o in out} == {"1", "2", "10"}
    assert all(o["rotulo"] == o["valor"] for o in out)  # no coluna_rotulo -> label = value


async def test_opcoes_de_no_rejeita_no_de_efeito():
    definicao = {
        "nos": [
            {"id": "fonte", "tipo": "sql", "sql": "SELECT 1 AS x"},
            {"id": "chama", "tipo": "http", "config": {"conexao_id": "x", "metodo": "GET"}},
        ],
        "arestas": [{"de": "fonte", "para": "chama"}],
    }
    with pytest.raises(ErroAplicacao) as ei:
        await opcoes_de_no(definicao, "chama", "x")
    assert ei.value.codigo == "derivacao_no_invalido"
