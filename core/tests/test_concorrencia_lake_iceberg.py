"""State shared between runs: merged index, Iceberg commit retry and per-source semaphore. Unit
level - no database, no lake, no Temporal."""

from __future__ import annotations

import asyncio

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import iceberg, leitura, service


def _p(particao: dict, caminho: str) -> dict:
    return {"particao": particao, "caminho": caminho, "linhas": 1, "bytes": 1}


def test_mesclar_indice_troca_o_escopo_desta_corrida_e_mantem_os_outros():
    atual = {
        "execucao_id": "A",
        "chaves": ["operacao_id", "project_id"],
        "particoes": [
            _p({"operacao_id": "61", "project_id": "1"}, "op61/1/A"),
            _p({"operacao_id": "62", "project_id": "9"}, "op62/9/A"),
        ],
    }
    corpo = {
        "execucao_id": "B",
        "chaves": ["operacao_id", "project_id"],
        "particoes": [_p({"operacao_id": "61", "project_id": "1"}, "op61/1/B")],
    }
    m = leitura._mesclar_indice(atual, corpo)
    assert m["execucao_id"] == "B"  # header = whoever touched it last
    caminhos = {tuple(sorted(p["particao"].items())): p["caminho"] for p in m["particoes"]}
    assert caminhos[(("operacao_id", "61"), ("project_id", "1"))] == "op61/1/B"  # replaced
    assert caminhos[(("operacao_id", "62"), ("project_id", "9"))] == "op62/9/A"  # inherited


def test_mesclar_indice_sem_atual_e_o_proprio_corpo():
    corpo = {"execucao_id": "B", "chaves": ["x"], "particoes": [_p({"x": "1"}, "c")]}
    assert leitura._mesclar_indice(None, corpo) is corpo


def test_chave_de_escopo_ignora_ordem_e_tipo():
    assert leitura._chave_escopo({"b": 2, "a": "1"}) == leitura._chave_escopo({"a": 1, "b": "2"})


async def test_commit_iceberg_retenta_recarregando_e_depois_desiste(monkeypatch):
    from pyiceberg.exceptions import CommitFailedException

    monkeypatch.setattr(iceberg, "ESPERA_COMMIT_S", (0, 0, 0))
    recargas = 0
    tentativas: list[object] = []

    def recarregar():
        nonlocal recargas
        recargas += 1
        return object()

    def fazer(tabela):
        tentativas.append(tabela)
        if len(tentativas) < 3:
            raise CommitFailedException("Table has been updated by another process")

    await iceberg._commitar_com_retry(fazer, recarregar, dataset="silver_x")
    assert len(tentativas) == 3
    assert recargas == 3  # reloaded BEFORE each attempt (precondition of the optimistic commit)
    assert len({id(t) for t in tentativas}) == 3  # never reused the stale table

    def sempre_falha(_t):
        raise CommitFailedException("conflict")

    with pytest.raises(ErroAplicacao) as exc:
        await iceberg._commitar_com_retry(sempre_falha, recarregar, dataset="silver_x")
    assert exc.value.codigo == "iceberg_commit_conflito" and exc.value.status == 409
    assert "silver_x" in exc.value.mensagem


async def test_semaforo_da_fonte_e_um_so_por_fonte_no_processo():
    a = service.semaforo_da_fonte("f1", 4)
    b = service.semaforo_da_fonte("f1", 4)
    c = service.semaforo_da_fonte("f2", 4)
    assert a is b and a is not c
    assert isinstance(a, asyncio.Semaphore)
