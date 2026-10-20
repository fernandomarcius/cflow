"""Teto PRÓPRIO da saída que só publica no lake (`PATCH_TETO_IO`), no `ExecutarPipeline`.

Puro: `workflow.patched` e `workflow.execute_activity` são dublês — sem servidor Temporal. O que se
prova: (1) a marca só é CONSULTADA quando a definição tem saída `lake` (pipeline sem publicação
segue byte-idêntico e um rollback do worker não quebra o replay dele); (2) com ela, as folhas
`gold_*` deixam de disputar o teto de 4 dos nós de CPU; (3) sem ela (workflow iniciado antes da
marca), tudo volta a dividir o teto de sempre.
"""

from __future__ import annotations

import asyncio

import pytest

from cflow_core.execucao.paralelo import MAX_NOS_EM_PARALELO
from cflow_worker import workflow as wf


def _definicao(n_gold: int, *, com_lake: bool = True) -> dict:
    nos = [{"id": "base", "tipo": "sql", "sql": "select 1"}]
    arestas = []
    for i in range(n_gold):
        destino = "lake" if com_lake else "arquivo"
        nos.append({"id": f"gold_{i}", "tipo": "saida", "config": {"destino": destino}})
        arestas.append({"de": "base", "para": f"gold_{i}"})
    return {"nos": nos, "arestas": arestas}


@pytest.fixture
def temporal(monkeypatch):
    estado = {"marcas": [], "em_voo": 0, "pico_gold": 0, "negar": set()}

    def _patched(marca: str) -> bool:
        estado["marcas"].append(marca)
        return marca not in estado["negar"]

    async def _executar(fn, args=None, **_kw):
        if fn is wf.executar_no_act:
            no_id = args[2]
            if no_id.startswith("gold_"):
                estado["em_voo"] += 1
                estado["pico_gold"] = max(estado["pico_gold"], estado["em_voo"])
                await asyncio.sleep(0.01)
                estado["em_voo"] -= 1
        return None

    monkeypatch.setattr(wf.workflow, "patched", _patched)
    monkeypatch.setattr(wf.workflow, "execute_activity", _executar)
    return estado


async def _rodar(definicao: dict) -> None:
    ordem = [n["id"] for n in definicao["nos"]]
    await wf.ExecutarPipeline().run("exec-1", definicao, ordem, {})


async def test_folhas_gold_passam_do_teto_de_cpu(temporal):
    await _rodar(_definicao(12))
    assert wf.PATCH_TETO_IO in temporal["marcas"]
    assert temporal["pico_gold"] > MAX_NOS_EM_PARALELO


async def test_sem_saida_lake_a_marca_nao_e_consultada(temporal):
    await _rodar(_definicao(6, com_lake=False))
    assert wf.PATCH_TETO_IO not in temporal["marcas"]


async def test_replay_anterior_a_marca_divide_o_teto_de_sempre(temporal):
    temporal["negar"] = {wf.PATCH_TETO_IO}
    await _rodar(_definicao(12))
    assert temporal["pico_gold"] == MAX_NOS_EM_PARALELO
