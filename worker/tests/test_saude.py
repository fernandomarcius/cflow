"""Sonda `/healthz`: 200 só com worker pollando, Temporal respondendo há pouco e sem drenagem."""

from __future__ import annotations

import asyncio

import pytest

from cflow_worker import saude


class _Relogio:
    def __init__(self) -> None:
        self.agora = 100.0

    def __call__(self) -> float:
        return self.agora


def _estado_saudavel(relogio: _Relogio | None = None) -> saude.EstadoSaude:
    estado = saude.EstadoSaude(relogio=relogio or _Relogio(), vence_apos=30)
    estado.observar_workers(lambda: True)
    estado.temporal_alcancavel()
    return estado


def test_saudavel_com_tudo_de_pe():
    assert _estado_saudavel().saudavel()


def test_nasce_indisponivel():
    """Sem worker observado nem verificação do Temporal: a sonda não pode mentir na subida."""
    assert not saude.EstadoSaude().saudavel()


def test_worker_parado_derruba():
    estado = _estado_saudavel()
    estado.observar_workers(lambda: False)
    assert not estado.saudavel()


def test_temporal_inalcancavel_derruba():
    estado = _estado_saudavel()
    estado.temporal_inalcancavel()
    assert not estado.saudavel()


def test_verificacao_velha_derruba():
    """Monitor travado (sem nova verificação) não pode deixar a sonda verde para sempre."""
    relogio = _Relogio()
    estado = _estado_saudavel(relogio)
    relogio.agora += 31
    assert not estado.saudavel()


def test_drenagem_derruba():
    estado = _estado_saudavel()
    estado.drenar()
    assert estado.drenando and not estado.saudavel()


async def test_monitor_marca_e_desmarca():
    estado = saude.EstadoSaude()
    estado.observar_workers(lambda: True)
    respostas = iter([True, False, True])
    passadas: list[bool] = []

    async def _verificar() -> bool:
        ok = next(respostas)
        passadas.append(ok)
        if len(passadas) == 3:
            raise asyncio.CancelledError
        return ok

    tarefa = asyncio.create_task(saude.monitorar(estado, _verificar, intervalo=0))
    with pytest.raises(asyncio.CancelledError):
        await tarefa
    assert passadas == [True, False, True]
    assert not estado.saudavel()  # a última passada que CONCLUIU disse "inalcançável"


async def test_monitor_trata_excecao_como_inalcancavel():
    estado = _estado_saudavel()
    chamadas = 0

    async def _verificar() -> bool:
        nonlocal chamadas
        chamadas += 1
        if chamadas > 1:
            raise asyncio.CancelledError
        raise RuntimeError("frontend fora")

    with pytest.raises(asyncio.CancelledError):
        await saude.monitorar(estado, _verificar, intervalo=0)
    assert not estado.saudavel()


async def _pedir(porta: int, linha: bytes) -> bytes:
    leitor, escritor = await asyncio.open_connection("127.0.0.1", porta)
    escritor.write(linha)
    await escritor.drain()
    resposta = await leitor.read()
    escritor.close()
    return resposta


@pytest.mark.parametrize(
    ("linha", "saudavel", "status"),
    [
        (b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n", True, b"200"),
        (b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n", False, b"503"),
        (b"GET /healthz?x=1 HTTP/1.1\r\n\r\n", True, b"200"),
        (b"HEAD /healthz HTTP/1.1\r\n\r\n", True, b"200"),
        (b"POST /healthz HTTP/1.1\r\n\r\n", True, b"405"),
        (b"GET /outra HTTP/1.1\r\n\r\n", True, b"404"),
        (b"lixo\r\n\r\n", True, b"400"),
    ],
)
async def test_servidor_http(linha, saudavel, status):
    """Liga um soquete de verdade em porta efêmera (loopback): é o que a plataforma sonda."""
    estado = _estado_saudavel() if saudavel else saude.EstadoSaude()
    servidor = await saude.servir(estado, "127.0.0.1", 0)
    porta = servidor.sockets[0].getsockname()[1]
    try:
        resposta = await _pedir(porta, linha)
    finally:
        servidor.close()
        await servidor.wait_closed()
    assert resposta.split(b" ", 2)[1] == status
    if linha.startswith(b"HEAD"):
        assert resposta.endswith(b"\r\n\r\n")  # HEAD sem corpo
