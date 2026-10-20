"""Generates the xlsx in a SEPARATE PROCESS.

The xlsx writer is pure Python (~0.9 us per cell); run in a thread, it holds the GIL of the
process that also executes the nodes, slowing every state write by more than an order of magnitude
on a micro-benchmark. In a child process (`spawn`) the same `gerar_xlsx` produces the same bytes
without touching anyone's GIL.

What crosses the process boundary must be serializable: progress goes through a small file (a
closure does not cross `spawn`) and `ErroAplicacao` comes back as data -- its `__init__` requires
`codigo` and `mensagem`, and the default pickle would rebuild it with the message only.
"""

from __future__ import annotations

import asyncio
import functools
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from ..config import get_settings
from ..errors import ErroAplicacao

_POOL: ProcessPoolExecutor | None = None


def tamanho_do_pool() -> int:
    """Pool size: `relatorios_concorrencia`, but at most HALF of the CPUs the process may use.
    Measured on a 4-CPU benchmark, 4 processes made some runs take twice the wall and CPU time,
    because the children compete for the same CPUs as the nodes' DuckDB instances; with 2 the runs
    were stable. Report activities beyond the pool wait in its queue, inside the activity itself.

    CPUs = the SMALLER of the affinity (cpuset) and the cgroup quota (`cpu.max`): with a CPU limit
    of 4 on a 32-core node the affinity says 32, and the pool would return to the pathological
    case."""
    from ..duck import cpus_cgroup

    try:
        cpus = len(os.sched_getaffinity(0))  # honors the cpuset (`cpu_count` does not)
    except AttributeError:  # macOS
        cpus = os.cpu_count() or 1
    cota = cpus_cgroup()
    if cota is not None:
        cpus = min(cpus, max(1, int(cota)))
    return max(1, min(int(get_settings().relatorios_concorrencia), cpus // 2))


def _pool() -> ProcessPoolExecutor:
    """Persistent pool of the parent process, created on the first generation (the cost of
    starting the interpreter and importing pyarrow/openpyxl is paid once per child, not per
    report)."""
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(
            max_workers=tamanho_do_pool(),
            mp_context=multiprocessing.get_context("spawn"),
        )
    return _POOL


def descartar_pool(pool: ProcessPoolExecutor | None = None) -> None:
    """Discards `pool` (default: the current one). Only resets the current one if it is THE SAME:
    a late `BrokenProcessPool` from an old pool must not take down the new, healthy pool -- its
    `cancel_futures` would raise `CancelledError` for the generations queued on it."""
    global _POOL
    alvo = pool if pool is not None else _POOL
    if alvo is None:
        return
    if _POOL is alvo:
        _POOL = None
    alvo.shutdown(wait=False, cancel_futures=True)


class ProgressoEmArquivo:
    """A `progresso(linhas)` callback that crosses processes: the child writes the count to a file,
    swapped in with `os.replace` (the reader never sees half a number)."""

    def __init__(self, caminho: str) -> None:
        self.caminho = caminho

    def __call__(self, linhas: int) -> None:
        tmp = f"{self.caminho}.tmp"
        with open(tmp, "w") as f:
            f.write(str(int(linhas)))
        os.replace(tmp, self.caminho)


def ler_progresso(caminho: str | Path) -> int | None:
    try:
        return int(Path(caminho).read_text() or 0)
    except (OSError, ValueError):
        return None


#: What the child may call in `gerar`: the name crosses `spawn`; the function does not.
FUNCOES = ("gerar_xlsx", "gerar_xlsx_agrupado")


def _gerar_no_filho(*args, funcao: str = "gerar_xlsx", **kwargs) -> tuple[str, object]:
    from . import gerar

    if funcao not in FUNCOES:
        raise ValueError(f"unknown generation function: {funcao}")
    try:
        return ("ok", getattr(gerar, funcao)(*args, **kwargs))
    except ErroAplicacao as exc:
        return ("erro", (exc.codigo, exc.mensagem, exc.status))


async def gerar(*args, progresso_em: str, funcao: str = "gerar_xlsx", **kwargs) -> dict:
    """Runs `gerar.<funcao>(*args, **kwargs)` in a pool process -- `gerar_xlsx` (one source) or
    `gerar_xlsx_agrupado`. A broken pool (a child killed for lack of memory, for example) is
    discarded and THIS generation falls back to a thread."""
    progresso = ProgressoEmArquivo(progresso_em)
    chamada = functools.partial(
        _gerar_no_filho, *args, funcao=funcao, progresso=progresso, **kwargs
    )
    pool = _pool()
    try:
        estado, valor = await asyncio.get_running_loop().run_in_executor(pool, chamada)
    except BrokenProcessPool:
        descartar_pool(pool)  # this one, not one that may have been created since
        estado, valor = await asyncio.to_thread(chamada)
    if estado == "erro":
        codigo, mensagem, status = valor
        raise ErroAplicacao(codigo=codigo, mensagem=mensagem, status=status)
    return valor
