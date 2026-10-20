"""Worker health probe: `GET /healthz` over HTTP/1.1 (port 8080 by default).

The worker has no business-facing HTTP surface, but container orchestrators (Kubernetes
readiness/liveness, Docker healthchecks) can only probe over HTTP, and a worker without a probe
that tells the truth is restarted for no reason or, worse, kept alive without a server. A few
lines of standard-library asyncio serve the probe: no web framework enters the image for it.

`/healthz` answers 200 only while ALL of the following hold:

- the workers of the process are running (polling their queues);
- the last gRPC health check against the Temporal frontend succeeded and is recent
  (`VENCE_APOS_SEGUNDOS`);
- the process is not draining to shut down.

Otherwise it answers 503: the platform restarts a worker that lost the server and stops counting
on one that is leaving. The probe itself NEVER calls Temporal: `monitorar` does, at a fixed rate,
so a burst of probes does not become a burst of RPCs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

logger = logging.getLogger("cflow_worker")

INTERVALO_VERIFICACAO_SEGUNDOS: Final = 10.0
TIMEOUT_VERIFICACAO_SEGUNDOS: Final = 5.0
VENCE_APOS_SEGUNDOS: Final = 3 * INTERVALO_VERIFICACAO_SEGUNDOS
TIMEOUT_LEITURA_SEGUNDOS: Final = 5.0
MAX_BYTES_LINHA: Final = 8 * 1024
MAX_LINHAS_CABECALHO: Final = 64

_MOTIVOS: Final = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
}


class EstadoSaude:
    """Prontidão compartilhada pela sonda, pelo monitor e pelo laço do worker."""

    def __init__(
        self,
        relogio: Callable[[], float] = time.monotonic,
        vence_apos: float = VENCE_APOS_SEGUNDOS,
    ) -> None:
        self._relogio = relogio
        self._vence_apos = vence_apos
        self._workers_rodando: Callable[[], bool] = lambda: False
        self._ultimo_ok: float | None = None
        self._drenando = False

    def observar_workers(self, rodando: Callable[[], bool]) -> None:
        self._workers_rodando = rodando

    def temporal_alcancavel(self) -> None:
        self._ultimo_ok = self._relogio()

    def temporal_inalcancavel(self) -> None:
        self._ultimo_ok = None

    def drenar(self) -> None:
        self._drenando = True

    @property
    def drenando(self) -> bool:
        return self._drenando

    def saudavel(self) -> bool:
        if self._drenando or self._ultimo_ok is None or not self._workers_rodando():
            return False
        return self._relogio() - self._ultimo_ok <= self._vence_apos


async def monitorar(
    estado: EstadoSaude,
    verificar: Callable[[], Awaitable[bool]],
    intervalo: float = INTERVALO_VERIFICACAO_SEGUNDOS,
    timeout: float = TIMEOUT_VERIFICACAO_SEGUNDOS,
) -> None:
    """Verifica o frontend do Temporal periodicamente, até ser cancelado. Loga só a TRANSIÇÃO
    (alcançável ↔ inalcançável): um log por verificação afogaria o resto a cada 10 s."""
    alcancavel: bool | None = None
    while True:
        try:
            ok = await asyncio.wait_for(verificar(), timeout)
        except Exception as erro:  # noqa: BLE001 — qualquer falha é "inalcançável"
            ok = False
            motivo = type(erro).__name__
        else:
            motivo = "not serving"
        if ok:
            estado.temporal_alcancavel()
        else:
            estado.temporal_inalcancavel()
        if ok != alcancavel:
            if ok:
                logger.info("temporal_alcancavel")
            else:
                logger.warning("temporal_inalcancavel", extra={"extra_campos": {"motivo": motivo}})
            alcancavel = ok
        await asyncio.sleep(intervalo)


async def _responder(escritor: asyncio.StreamWriter, status: int, corpo: bytes, head: bool) -> None:
    cabecalho = (
        f"HTTP/1.1 {status} {_MOTIVOS[status]}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(corpo)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    escritor.write(cabecalho if head else cabecalho + corpo)
    await escritor.drain()


async def _ler_requisicao(leitor: asyncio.StreamReader) -> tuple[str, str] | None:
    linha = await leitor.readline()
    partes = linha.decode("latin-1").split()
    if len(partes) != 3 or not partes[2].startswith("HTTP/"):
        return None
    for _ in range(MAX_LINHAS_CABECALHO):
        cabecalho = await leitor.readline()
        if cabecalho in (b"\r\n", b"\n", b""):
            return partes[0], partes[1]
    return None


def tratador(
    estado: EstadoSaude,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def tratar(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        try:
            try:
                requisicao = await asyncio.wait_for(
                    _ler_requisicao(leitor), TIMEOUT_LEITURA_SEGUNDOS
                )
            except (TimeoutError, ValueError, asyncio.LimitOverrunError):
                requisicao = None
            if requisicao is None:
                await _responder(escritor, 400, b"bad request\n", head=False)
                return
            metodo, alvo = requisicao
            head = metodo == "HEAD"
            if alvo.split("?", 1)[0] != "/healthz":
                await _responder(escritor, 404, b"not found\n", head)
            elif metodo not in {"GET", "HEAD"}:
                await _responder(escritor, 405, b"method not allowed\n", head)
            elif estado.saudavel():
                await _responder(escritor, 200, b"ok\n", head)
            else:
                await _responder(escritor, 503, b"unavailable\n", head)
        except ConnectionError:
            pass
        finally:
            escritor.close()
            with contextlib.suppress(ConnectionError):
                await escritor.wait_closed()

    return tratar


async def servir(estado: EstadoSaude, host: str, porta: int) -> asyncio.Server:
    """Sobe o servidor da sonda; quem chama o fecha."""
    return await asyncio.start_server(tratador(estado), host, porta, limit=MAX_BYTES_LINHA)
