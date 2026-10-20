"""Ciclo de vida do processo: configuração, conexão com recuo e DRENAGEM no SIGTERM.

Tudo com dublês: nenhum Temporal, banco ou rede. O que se prova é a ORDEM — sonda 503 antes de
parar de pollar, laço cadenciado terminando a passada em vez de ser cortado no meio, código de
saída distinguindo encerramento pedido de worker que caiu sozinho.
"""

from __future__ import annotations

import asyncio

import pytest

from cflow_worker import main as worker_main
from cflow_worker import saude

# ---------------------------------------------------------------------------------------------
# config_processo
# ---------------------------------------------------------------------------------------------


def test_config_padrao():
    cfg = worker_main.config_processo({})
    assert cfg.porta_saude == 8080 and cfg.drenagem_segundos == 20


def test_config_zero_desliga_a_sonda():
    assert worker_main.config_processo({"CFLOW_HEALTHZ_PORTA": "0"}).porta_saude is None


def test_config_nomeia_todos_os_erros_de_uma_vez():
    with pytest.raises(worker_main.ErroConfiguracao) as erro:
        worker_main.config_processo(
            {"CFLOW_HEALTHZ_PORTA": "oito", "CFLOW_WORKER_DRENAGEM_SEGUNDOS": "-1"}
        )
    assert "CFLOW_HEALTHZ_PORTA" in str(erro.value)
    assert "CFLOW_WORKER_DRENAGEM_SEGUNDOS" in str(erro.value)


def test_config_metricas_na_mesma_porta_da_sonda():
    with pytest.raises(worker_main.ErroConfiguracao, match="CFLOW_METRICAS_PORTA"):
        worker_main.config_processo({"CFLOW_HEALTHZ_PORTA": "9464"}, metricas_porta=9464)
    # Sonda desligada libera a porta para as métricas.
    worker_main.config_processo({"CFLOW_HEALTHZ_PORTA": "0"}, metricas_porta=8080)


# ---------------------------------------------------------------------------------------------
# conectar
# ---------------------------------------------------------------------------------------------


async def test_conectar_retenta_ate_conseguir(monkeypatch):
    monkeypatch.setattr(worker_main, "RECONEXAO_TETO_SEGUNDOS", 0.01)
    tentativas = 0

    async def _conectar(host, runtime=None):
        nonlocal tentativas
        tentativas += 1
        if tentativas < 3:
            raise RuntimeError("connection refused")
        return f"cliente:{host}"

    parar = asyncio.Event()
    cliente = await worker_main.conectar("temporal:8080", parar, conectar_fn=_conectar)
    assert cliente == "cliente:temporal:8080" and tentativas == 3


async def test_conectar_desiste_quando_pedem_para_parar():
    async def _conectar(host, runtime=None):
        parar.set()  # o SIGTERM chegou durante a tentativa
        raise RuntimeError("connection refused")

    parar = asyncio.Event()
    assert await worker_main.conectar("temporal:8080", parar, conectar_fn=_conectar) is None


# ---------------------------------------------------------------------------------------------
# _repetir com parada
# ---------------------------------------------------------------------------------------------


async def test_repetir_com_parar_termina_depois_da_passada_corrente():
    parar = asyncio.Event()
    passadas: list[str] = []

    async def _tick():
        passadas.append("inicio")
        parar.set()  # sinal chega NO MEIO da passada
        await asyncio.sleep(0)
        passadas.append("fim")

    await asyncio.wait_for(worker_main._repetir(_tick, 3600, parar), 1)
    assert passadas == ["inicio", "fim"]  # terminou a passada e não esperou o intervalo


# ---------------------------------------------------------------------------------------------
# rodar_ate_parar
# ---------------------------------------------------------------------------------------------


class _WorkerFalso:
    """Imita o `temporalio.worker.Worker`: `run()` até `shutdown()`, que espera a drenagem."""

    def __init__(
        self,
        nome: str,
        eventos: list[str],
        cai_com: Exception | None = None,
        estado: saude.EstadoSaude | None = None,
    ):
        self.nome = nome
        self.estado = estado
        self.eventos = eventos
        self.cai_com = cai_com
        self.is_running = False
        self._fim = asyncio.Event()

    async def run(self) -> None:
        self.is_running = True
        try:
            if self.cai_com is not None:
                await asyncio.sleep(0)
                raise self.cai_com
            await self._fim.wait()
        finally:
            self.is_running = False

    async def shutdown(self) -> None:
        sonda = "?" if self.estado is None else ("503" if not self.estado.saudavel() else "200")
        self.eventos.append(f"shutdown:{self.nome}:sonda={sonda}")
        self._fim.set()


async def _ok() -> bool:
    return True


async def test_sigterm_drena_na_ordem_e_sai_zero():
    eventos: list[str] = []
    parar = asyncio.Event()
    estado = saude.EstadoSaude()
    workers = [
        _WorkerFalso("execucao", eventos, estado=estado),
        _WorkerFalso("ingestao", eventos, estado=estado),
    ]

    async def _laco():
        await parar.wait()
        eventos.append("laco_viu_parar")

    laco = asyncio.create_task(_laco())

    async def _sigterm():
        for _ in range(100):  # espera a sonda ficar verde antes de mandar o sinal
            if estado.saudavel():
                break
            await asyncio.sleep(0.01)
        eventos.append(f"sonda_antes:{estado.saudavel()}")
        parar.set()

    asyncio.create_task(_sigterm())
    codigo = await asyncio.wait_for(
        worker_main.rodar_ate_parar(workers, parar, estado, _ok, [laco], 1), 5
    )
    assert codigo == 0
    assert eventos[0] == "sonda_antes:True"
    assert "laco_viu_parar" in eventos
    # A sonda já era 503 quando os workers pararam de pollar: a plataforma tira o pod do ar antes.
    assert {"shutdown:execucao:sonda=503", "shutdown:ingestao:sonda=503"} <= set(eventos)
    assert not estado.saudavel()


async def test_worker_que_cai_sozinho_sai_um_e_para_os_outros():
    eventos: list[str] = []
    parar = asyncio.Event()
    workers = [
        _WorkerFalso("execucao", eventos),
        _WorkerFalso("relatorios", eventos, cai_com=RuntimeError("poller morreu")),
    ]
    codigo = await asyncio.wait_for(
        worker_main.rodar_ate_parar(workers, parar, saude.EstadoSaude(), _ok, (), 1), 5
    )
    assert codigo == 1
    assert parar.is_set()  # os laços cadenciados recebem a parada também
    assert "shutdown:execucao:sonda=?" in eventos


async def test_laco_que_nao_termina_e_cancelado_depois_da_drenagem():
    parar = asyncio.Event()
    cancelado = asyncio.Event()

    async def _laco_teimoso():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelado.set()
            raise

    laco = asyncio.create_task(_laco_teimoso())
    parar.set()
    codigo = await asyncio.wait_for(
        worker_main.rodar_ate_parar(
            [_WorkerFalso("execucao", [])], parar, saude.EstadoSaude(), _ok, [laco], 0
        ),
        5,
    )
    assert codigo == 0 and cancelado.is_set()
