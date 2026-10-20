"""`montante_publica: false` on the ROOT of a chained dispatch - the pure pieces.

A root pipeline reads an upstream pipeline through an `origem: "pipeline"` source, and chaining
reruns the upstream chain at ITS as-of. Those runs used to publish: the `_atual.json` that a
reference reads as FROZEN history and the dataset pointer that other pipelines read through
`publicado` changed owner depending on who finished last. The flag makes the upstream compute and
write a snapshot (what the root reads through the freshness map) without touching anything another
consumer reads.

Here: validation of the key, the decision from the ROOT definition, the refusal of upstream
effects, and `_publicar_saida`, which skips publishing but keeps the empty guard. The path with
database and worker is in `api/tests/integration/test_montante_sem_publicar.py`.
"""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import disparo, engine
from cflow_core.pipelines import definicao as d


def _def(**extra) -> dict:
    return {"nos": [{"id": "a", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": [], **extra}


# --- key validation ------------------------------------------------------------------------


@pytest.mark.parametrize("valor", [True, False])
def test_validar_aceita_bool(valor):
    d.validar(_def(montante_publica=valor))


@pytest.mark.parametrize("valor", ["false", 0, 1, None, [], {"x": 1}])
def test_validar_recusa_nao_bool(valor):
    """`"false"` is truthy in any `if`, and `0`/`1` would pass as bool in a loose test: the flag
    decides whether the run publishes, so only the JSON literal `true`/`false` counts."""
    with pytest.raises(ErroAplicacao) as exc:
        d.validar(_def(montante_publica=valor))
    assert exc.value.status == 422
    assert any(det.campo == "montante_publica" for det in exc.value.detalhes)


def test_ausente_e_publicar_como_sempre():
    assert d.montante_publica(_def()) is True
    assert d.montante_publica(_def(montante_publica=True)) is True
    assert d.montante_publica(_def(montante_publica=False)) is False


# --- decision at dispatch: from the ROOT definition -----------------------------------------------


def _item(pid: str, definicao: dict, nivel: int) -> dict:
    return {"pipeline_id": pid, "versao_numero": 1, "definicao": definicao, "nivel": nivel}


def _ordem(raiz_def: dict, montante_def: dict | None = None) -> list[dict]:
    return [_item("w", montante_def or _def(), 0), _item("r", raiz_def, 1)]


def test_raiz_com_marca_desliga_publicacao_da_montante():
    assert disparo.decidir_montante(_ordem(_def(montante_publica=False)), "r") is True


def test_raiz_sem_marca_degenera_no_comportamento_de_hoje():
    assert disparo.decidir_montante(_ordem(_def()), "r") is False
    assert disparo.decidir_montante(_ordem(_def(montante_publica=True)), "r") is False


def test_marca_numa_montante_nao_vale():
    """The flag belongs to the REQUESTED run. An upstream that declares it keeps publishing when it
    is the one running by table, and it only counts when it is itself the dispatch root."""
    ordem = _ordem(_def(), _def(montante_publica=False))
    assert disparo.decidir_montante(ordem, "r") is False


def _com_efeito(tipo: str) -> dict:
    return {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {"id": "efx", "tipo": tipo, "config": {}},
        ],
        "arestas": [{"de": "src", "para": "efx"}],
    }


@pytest.mark.parametrize("tipo", ["escrita_sql", "http", "email", "ingestao"])
def test_efeito_a_montante_com_marca_e_recusa_nomeada(tipo):
    ordem = _ordem(_def(montante_publica=False), _com_efeito(tipo))
    with pytest.raises(ErroAplicacao) as exc:
        disparo.decidir_montante(ordem, "r", nomes={"w": "Load X"})
    assert exc.value.status == 400
    assert exc.value.codigo == "montante_com_efeito"
    assert "Load X" in exc.value.mensagem and "efx" in exc.value.mensagem
    assert tipo in exc.value.detalhes[0].mensagem


def test_efeito_na_propria_raiz_nao_e_recusado():
    """The root publishes normally - including its own effects. The flag only concerns upstream."""
    raiz = {**_com_efeito("email"), "montante_publica": False}
    assert disparo.decidir_montante(_ordem(raiz), "r") is True


def test_efeito_a_montante_sem_marca_segue_como_hoje():
    assert disparo.decidir_montante(_ordem(_def(), _com_efeito("escrita_sql")), "r") is False


# --- engine: `_publicar_saida(sem_publicar=True)` ---------------------------------------------


def _duck_com(linhas: int):
    duck = duckdb.connect()
    duck.execute(f'CREATE TABLE "out" AS SELECT range AS n FROM range({linhas})')
    return duck


def test_sem_publicar_nao_grava_no_export_dir(tmp_path):
    no = {"id": "out", "tipo": "saida", "config": {"nome": "ds_x", "caminho": str(tmp_path)}}
    res = asyncio.run(engine._publicar_saida(_duck_com(2), no, "out", 2, sem_publicar=True))
    assert res is None
    assert list(tmp_path.iterdir()) == []


def test_sem_publicar_nao_toca_o_lake(tmp_path, monkeypatch):
    """Returns BEFORE opening the storage: no object, no pointer, no index."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        no = {
            "id": "out",
            "tipo": "saida",
            "config": {
                "destino": "lake",
                "camada": "gold",
                "dataset": "ds_x",
                "particao": [{"origem": "parametro", "nome": "operacao_id"}],
            },
        }
        res = asyncio.run(
            engine._publicar_saida(
                _duck_com(3), no, "out", 3, parametros={"operacao_id": "61"}, sem_publicar=True
            )
        )
        assert res is None
        assert not (tmp_path / "lake").exists()
    finally:
        get_settings.cache_clear()


def test_sem_publicar_mantem_o_guard_de_vazio(tmp_path):
    """Not publishing is not "not checking": an empty upstream still fails at the output node, which
    keeps the root from computing over an input that a normal run would refuse."""
    no = {"id": "out", "tipo": "saida", "config": {"nome": "ds_x", "caminho": str(tmp_path)}}
    with pytest.raises(ErroAplicacao) as exc:
        asyncio.run(engine._publicar_saida(_duck_com(0), no, "out", 0, sem_publicar=True))
    assert exc.value.codigo == "saida_vazia"
