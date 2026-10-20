"""Pipeline promotion between environments: reference rewriting and the APPLY ORDER."""

from __future__ import annotations

import pytest

from cflow_core.pipelines.promocao import (
    CicloDePipelines,
    ReferenciaAusente,
    ordem_topologica,
    reescrever,
    refs_conexao,
    refs_pipeline,
    slug,
)


def _def(nos: list[dict]) -> dict:
    return {"nos": nos, "arestas": []}


def test_acha_as_referencias_sem_repetir():
    d = _def(
        [
            {"id": "a", "config": {"pipeline_id": "P1", "no_id": "saida"}},
            {"id": "b", "config": {"pipeline_id": "P1", "no_id": "orders"}},
            {"id": "c", "config": {"conexao_id": "C1"}},
            {"id": "d", "config": {"conexao_id": "C1"}},
            {"id": "e", "sql": "select 1"},
        ]
    )
    assert refs_pipeline(d) == ["P1"]
    assert refs_conexao(d) == ["C1"]


def test_reescreve_nos_dois_sentidos_com_o_mapa_invertido():
    """Export swaps id -> name; import swaps name -> id. It is the SAME function."""
    d = _def([{"id": "f", "config": {"pipeline_id": "P1", "conexao_id": "C1"}}])
    exportada, _ = reescrever(
        d, mapa_conexoes={"C1": "SourceDb"}, mapa_pipelines={"P1": "orders_load"}
    )
    assert exportada["nos"][0]["config"] == {"pipeline_id": "orders_load", "conexao_id": "SourceDb"}
    importada, _ = reescrever(
        exportada, mapa_conexoes={"SourceDb": "C9"}, mapa_pipelines={"orders_load": "P9"}
    )
    assert importada["nos"][0]["config"] == {"pipeline_id": "P9", "conexao_id": "C9"}


def test_referencia_sem_correspondente_e_erro_nomeando_o_no():
    d = _def([{"id": "source_orders", "config": {"pipeline_id": "P1"}}])
    with pytest.raises(ReferenciaAusente, match="source_orders"):
        reescrever(d, mapa_conexoes={}, mapa_pipelines={})


def test_a_definicao_de_entrada_nao_e_alterada():
    d = _def([{"id": "f", "config": {"conexao_id": "C1"}}])
    reescrever(d, mapa_conexoes={"C1": "SourceDb"}, mapa_pipelines={})
    assert d["nos"][0]["config"]["conexao_id"] == "C1"


def test_ordem_poe_a_dependencia_ANTES_de_quem_depende():
    """The file is applied top to bottom: a root before the pipeline it reads fails on the target
    after having already created the earlier ones."""
    deps = {"report_03": ["load_b"], "load_b": ["load_a"], "load_a": [], "report_00": []}
    ordem = ordem_topologica(deps, ["report_00", "report_03"])
    assert ordem.index("load_a") < ordem.index("load_b") < ordem.index("report_03")
    assert set(ordem) == set(deps)


def test_cada_pipeline_aparece_uma_vez_mesmo_com_varias_raizes():
    deps = {"a": ["shared"], "b": ["shared"], "shared": []}
    ordem = ordem_topologica(deps, ["a", "b"])
    assert ordem.count("shared") == 1
    assert ordem.index("shared") == 0


def test_ciclo_nomeia_os_dois_lados_em_vez_de_estourar_a_pilha():
    with pytest.raises(CicloDePipelines, match="a -> b -> a"):
        ordem_topologica({"a": ["b"], "b": ["a"]}, ["a"])


def test_slug_para_nome_de_dataset():
    assert slug("Orders LOAD_DAILY") == "orders_load_daily"
    assert slug("Report 1 · Net Value") == "report_1_net_value"


def test_a_biblioteca_de_funcoes_tambem_e_remapeada_por_nome():
    """The library id CHANGES between environments. Promoting without remapping carries an id that
    does not exist on the other side, and the pipeline only breaks at EXECUTION ("Scalar Function
    ... does not exist"); validation passes, because the definition is well formed."""
    d = {"nos": [], "arestas": [], "bibliotecas": ["ID-DEV"]}
    exportada, notas = reescrever(
        d, mapa_conexoes={}, mapa_pipelines={}, mapa_bibliotecas={"ID-DEV": "MathLib"}
    )
    assert exportada["bibliotecas"] == ["MathLib"]
    assert any("library" in n for n in notas)
    importada, _ = reescrever(
        exportada,
        mapa_conexoes={},
        mapa_pipelines={},
        mapa_bibliotecas={"MathLib": "ID-PRD"},
    )
    assert importada["bibliotecas"] == ["ID-PRD"]


def test_biblioteca_sem_correspondente_e_erro_nomeado():
    with pytest.raises(ReferenciaAusente, match="library"):
        reescrever(
            {"nos": [], "arestas": [], "bibliotecas": ["X"]},
            mapa_conexoes={},
            mapa_pipelines={},
            mapa_bibliotecas={},
        )


def test_sem_mapa_de_biblioteca_a_definicao_passa_intacta():
    """Without the map, the field is left untouched."""
    d = {"nos": [], "arestas": [], "bibliotecas": ["ID"]}
    saida, _ = reescrever(d, mapa_conexoes={}, mapa_pipelines={})
    assert saida["bibliotecas"] == ["ID"]
