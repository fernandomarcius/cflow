"""A run that ends in FAILURE must emit `ingestao_execucao_concluida`.

The inline path always emitted it. The Temporal activity called `repo.finalizar_execucao` DIRECTLY:
the database recorded the failure and the log said nothing. Since `CFLOW_USAR_TEMPORAL=true` is the
default, **the only failure path that runs in production was the blind one** - exactly on the
event someone comes to investigate. The hole did not show up in tests because the tests that
exercised failure used inline mode.

`service.falhar` is the pair of `service.concluir`: it closes in the database **and** in the log.
"""

from __future__ import annotations

import logging

import pytest

from cflow_core.ingestao import service


class _RepoFalso:
    def __init__(self) -> None:
        self.chamadas: list[dict] = []

    async def finalizar_execucao(self, execucao_id, *, contagens, erro=None):
        self.chamadas.append({"execucao_id": execucao_id, "contagens": contagens, "erro": erro})


@pytest.fixture
def repo_falso(monkeypatch):
    r = _RepoFalso()
    monkeypatch.setattr(service.repo, "finalizar_execucao", r.finalizar_execucao)
    return r


async def test_falhar_grava_no_banco_e_emite_o_evento(repo_falso, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=service.logger.name):
        await service.falhar(
            execucao_id="11111111-1111-1111-1111-111111111111",
            erro="lake unavailable",
            contagens={"novo": 8, "quarentena": 1},
            fonte_id="22222222-2222-2222-2222-222222222222",
            dataset="registry_monthly",
            fatias=10,
            duracao_ms=1234,
        )

    assert len(repo_falso.chamadas) == 1, "the failure must be written to the database"
    assert repo_falso.chamadas[0]["erro"] == "lake unavailable"

    eventos = [r for r in caplog.records if r.getMessage() == "ingestao_execucao_concluida"]
    assert eventos, "the closing event was not emitted - it is what gets investigated"
    campos = eventos[0].extra_campos
    assert campos["status"] == "falha"
    assert campos["execucao_id"] == "11111111-1111-1111-1111-111111111111"
    assert campos["dataset"] == "registry_monthly"
    assert campos["fatias"] == 10
    assert campos["duracao_ms"] == 1234
    assert campos["motivo"] == "lake unavailable"


async def test_falha_e_WARNING_nao_INFO(repo_falso, caplog) -> None:
    """A failure at INFO level gets lost in the ingestion volume - many sources, hundreds of slices."""
    with caplog.at_level(logging.INFO, logger=service.logger.name):
        await service.falhar(execucao_id="x", erro="boom", contagens={})
    ev = [r for r in caplog.records if r.getMessage() == "ingestao_execucao_concluida"]
    assert ev and ev[0].levelno == logging.WARNING


async def test_as_contagens_do_que_ja_pousou_sobrevivem_a_falha(repo_falso) -> None:
    """8 of 10 slices is not 'zero'. What landed stays recorded; only the watermark does not advance."""
    await service.falhar(execucao_id="x", erro="boom", contagens={"novo": 8, "quarentena": 1})
    resumo = repo_falso.chamadas[0]["contagens"]
    assert resumo == service.totais({"novo": 8, "quarentena": 1})
    assert resumo["novos"] == 8 and resumo["quarentena"] == 1 and resumo["lotes"] == 9


async def test_sem_fonte_e_dataset_o_evento_AINDA_sai(repo_falso, caplog) -> None:
    """The Temporal activity knows the run, not the source.

    A final line with missing fields is much better than a missing final line: `execucao_id` ties
    this line to `ingestao_execucao_iniciada`, which already carried source and dataset.
    """
    with caplog.at_level(logging.WARNING, logger=service.logger.name):
        await service.falhar(execucao_id="only-the-run", erro="boom", contagens={})
    ev = [r for r in caplog.records if r.getMessage() == "ingestao_execucao_concluida"]
    assert ev, "without source/dataset the event must not vanish"
    assert ev[0].extra_campos["execucao_id"] == "only-the-run"
    assert ev[0].extra_campos["dataset"] is None


async def test_o_erro_e_saneado_e_truncado(repo_falso, caplog) -> None:
    """An error message may carry origin payload - it goes out sanitized and capped at 200 chars."""
    with caplog.at_level(logging.WARNING, logger=service.logger.name):
        await service.falhar(execucao_id="x", erro="E" * 500, contagens={})
    ev = [r for r in caplog.records if r.getMessage() == "ingestao_execucao_concluida"]
    assert len(ev[0].extra_campos["motivo"]) <= 200
