"""Os dois laços cadenciados do worker, agora testáveis (eram closures dentro de `main()`).

O que estes testes provam é o defeito que motivou a extração: uma fonte que levanta no meio da
lista não pode levar as seguintes junto. `fontes_devidas` já avançou o `proxima_em` de TODAS antes
de devolvê-las, então a fonte não processada não volta no tick seguinte — só na próxima cadência.
"""

from __future__ import annotations

import pytest

from cflow_worker import main as worker_main

# ---------------------------------------------------------------------------------------------
# tick_ingestao
# ---------------------------------------------------------------------------------------------


async def test_ingestao_uma_fonte_falha_e_as_demais_seguem():
    """A 2ª de 3 levanta: a 3ª TEM de ser disparada, e o retorno acusa 1 erro."""
    devidas = [
        {"id": "f1", "nome": "cvm_fi_diario"},
        {"id": "f2", "nome": "b3_cotahist_diario"},
        {"id": "f3", "nome": "anbima_debentures"},
    ]
    chamadas: list[str] = []

    async def _executar(*, fonte_id, origem, ator_id):
        chamadas.append(fonte_id)
        assert origem == "agendada" and ator_id is None
        if fonte_id == "f2":
            raise RuntimeError("origem devolveu 500")

    async def _listar():
        return devidas

    resumo = await worker_main.tick_ingestao(_executar, _listar)

    assert chamadas == ["f1", "f2", "f3"]  # a 3ª rodou mesmo com a 2ª quebrada
    assert resumo == {"disparadas": 2, "erros": 1}


async def test_ingestao_sem_devidas_nao_chama_nada():
    async def _executar(**_kw):  # pragma: no cover — não deve ser chamado
        raise AssertionError("não havia fonte devida")

    async def _listar():
        return []

    assert await worker_main.tick_ingestao(_executar, _listar) == {"disparadas": 0, "erros": 0}


async def test_ingestao_listar_devidas_quebrado_nao_propaga():
    """Banco fora derruba `fontes_devidas`; o laço do worker é um `while True` e não pode morrer."""

    async def _executar(**_kw):  # pragma: no cover
        raise AssertionError("não deveria chegar aqui")

    async def _listar():
        raise ConnectionError("banco fora")

    assert await worker_main.tick_ingestao(_executar, _listar) == {"disparadas": 0, "erros": 0}


async def test_ingestao_todas_falham_conta_todas():
    async def _executar(*, fonte_id, origem, ator_id):
        raise RuntimeError("falhou")

    async def _listar():
        return [{"id": "a", "nome": "a"}, {"id": "b", "nome": "b"}]

    assert await worker_main.tick_ingestao(_executar, _listar) == {"disparadas": 0, "erros": 2}


# ---------------------------------------------------------------------------------------------
# tick_agendador
# ---------------------------------------------------------------------------------------------


async def test_agendador_erro_nao_propaga(monkeypatch):
    """`processar_devidos` levantando não pode escapar: escapando, mata o agendador do processo
    inteiro em silêncio — o worker segue saudável e nenhum pipeline agendado roda mais."""

    async def _quebrado(_dispatch):
        raise RuntimeError("banco fora")

    monkeypatch.setattr("cflow_core.execucao.agendador.processar_devidos", _quebrado)

    async def _dispatch(*_a, **_kw):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    assert await worker_main.tick_agendador(_dispatch) == 0


async def test_agendador_devolve_quantos_disparou(monkeypatch):
    async def _ok(dispatch):
        assert dispatch is _dispatch
        return 3

    monkeypatch.setattr("cflow_core.execucao.agendador.processar_devidos", _ok)

    async def _dispatch(*_a, **_kw):  # pragma: no cover
        raise AssertionError("o dispatch é usado por processar_devidos, não pelo tick")

    assert await worker_main.tick_agendador(_dispatch) == 3


# ---------------------------------------------------------------------------------------------
# _repetir
# ---------------------------------------------------------------------------------------------


async def test_repetir_dorme_o_intervalo_entre_passadas(monkeypatch):
    """O laço é `tick` → `sleep`, sempre nessa ordem. Sem o sleep, um tick barato vira busy loop."""
    eventos: list[str] = []

    async def _tick():
        eventos.append("tick")
        if eventos.count("tick") == 3:
            raise StopAsyncIteration

    async def _sleep(segundos):
        eventos.append(f"sleep:{segundos}")

    monkeypatch.setattr(worker_main.asyncio, "sleep", _sleep)
    with pytest.raises(StopAsyncIteration):
        await worker_main._repetir(_tick, 42)

    assert eventos == ["tick", "sleep:42", "tick", "sleep:42", "tick"]


# ---------------------------------------------------------------------------------------------
# tick_reconciliacao
# ---------------------------------------------------------------------------------------------


async def test_reconciliacao_devolve_o_resumo_e_passa_a_idade_minima():
    visto: dict = {}

    async def _reconciliar(*, idade_minima_minutos):
        visto["idade"] = idade_minima_minutos
        return {"fechadas": 2, "vivas": 1}

    r = await worker_main.tick_reconciliacao(_reconciliar)

    assert r == {"fechadas": 2, "vivas": 1}
    assert visto["idade"] == worker_main.IDADE_ORFA_MINUTOS


async def test_reconciliacao_com_erro_nao_propaga():
    """O laço é `while True`: uma passada que levanta mataria o reaper do processo em silêncio."""

    async def _reconciliar(**_kw):
        raise ConnectionError("banco fora")

    r = await worker_main.tick_reconciliacao(_reconciliar)

    assert r["erro"] is True and r["fechadas"] == 0
