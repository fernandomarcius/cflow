"""Edges of an ALREADY EXECUTED run (the run map), rebuilt from the definitions.

The trigger does not store the chain's `deps`; the map derives them again from the versions that
ran. What is proven: the same two kinds of edge as the trigger (`pipeline` source and `publicado`
read of the chain's producer), only between members of the run, and bridging of hidden nodes --
without it, two pipelines that depend on each other would look independent to someone who cannot
see the one in the middle.
"""

from __future__ import annotations

from cflow_core.pipelines.dag import arestas_da_cadeia, restringir_arestas


def _fonte_pipeline(pid: str) -> dict:
    return {
        "id": f"src_{pid}",
        "tipo": "fonte",
        "config": {"origem": "pipeline", "pipeline_id": pid, "no_id": "x"},
    }


def _publica(dataset: str) -> dict:
    return {
        "id": "saida",
        "tipo": "saida",
        "config": {"destino": "lake", "camada": "gold", "dataset": dataset},
    }


def _le(dataset: str) -> dict:
    return {"id": "pub", "tipo": "fonte", "config": {"origem": "publicado", "dataset": dataset}}


def _item(pid: str, *nos: dict) -> dict:
    return {"pipeline_id": pid, "definicao": {"nos": list(nos), "arestas": []}}


def test_fonte_pipeline_vira_aresta():
    arestas = arestas_da_cadeia([_item("A"), _item("B", _fonte_pipeline("A"))], raiz="B")
    assert arestas == {"A": {}, "B": {"A": "pipeline"}}


def test_so_a_dependencia_direta_nao_o_fecho():
    arestas = arestas_da_cadeia(
        [_item("A"), _item("B", _fonte_pipeline("A")), _item("C", _fonte_pipeline("B"))], raiz="C"
    )
    assert arestas["C"] == {"B": "pipeline"}
    assert arestas["B"] == {"A": "pipeline"}


def test_dependencia_fora_da_corrida_e_ignorada():
    # B reads X, but X did not run in this run: the map only links members of the run.
    assert arestas_da_cadeia([_item("B", _fonte_pipeline("X"))]) == {"B": {}}


def test_leitura_publicado_espera_o_produtor_da_corrida():
    arestas = arestas_da_cadeia([_item("W", _publica("ds")), _item("R", _le("ds"))], raiz="R")
    assert arestas["R"] == {"W": "publicado"}
    assert arestas["W"] == {}


def test_publicado_sem_produtor_na_corrida_nao_liga():
    assert arestas_da_cadeia([_item("R", _le("ds"))]) == {"R": {}}


def test_implicita_que_fecharia_ciclo_e_descartada():
    # W depends on R through a `pipeline` source (explicit); R reads what W publishes. The implicit
    # R->W edge would close a cycle -- the explicit one wins, same rule as the trigger.
    arestas = arestas_da_cadeia(
        [_item("W", _fonte_pipeline("R"), _publica("ds")), _item("R", _le("ds"))], raiz="W"
    )
    assert arestas == {"R": {}, "W": {"R": "pipeline"}}


def test_explicita_prevalece_sobre_publicado_no_mesmo_par():
    arestas = arestas_da_cadeia(
        [_item("W", _publica("ds")), _item("R", _fonte_pipeline("W"), _le("ds"))], raiz="R"
    )
    assert arestas["R"] == {"W": "pipeline"}


def test_no_escondido_e_ponteado():
    todas = {"A": {}, "H": {"A": "pipeline"}, "C": {"H": "pipeline"}}
    assert restringir_arestas(todas, {"A", "C"}) == {"A": {}, "C": {"A": "pipeline"}}


def test_ponte_com_trecho_publicado_vira_publicado():
    todas = {"A": {}, "H": {"A": "publicado"}, "C": {"H": "pipeline"}}
    assert restringir_arestas(todas, {"A", "C"})["C"] == {"A": "publicado"}


def test_ponte_prefere_o_caminho_todo_pipeline():
    # Two A->C paths: one only through `pipeline` sources, the other through a `publicado` read.
    todas = {
        "A": {},
        "H1": {"A": "pipeline"},
        "H2": {"A": "publicado"},
        "C": {"H1": "pipeline", "H2": "pipeline"},
    }
    assert restringir_arestas(todas, {"A", "C"})["C"] == {"A": "pipeline"}


def test_restringir_sem_nada_escondido_e_identidade():
    todas = {"A": {}, "B": {"A": "pipeline"}}
    assert restringir_arestas(todas, {"A", "B"}) == todas
