"""Dedicated executor for lake I/O: what it must guarantee to avoid being just one more pool.

The point of the separation is that the lake thread stays BLOCKED on disk/network while the
DuckDB one holds a CPU slot - mixing them made I/O waiting consume a work slot."""

from __future__ import annotations

import asyncio
import threading

import pytest

from cflow_core import io_lake
from cflow_core.config import get_settings


@pytest.fixture(autouse=True)
def _limpar():
    io_lake.descartar()
    get_settings.cache_clear()
    yield
    io_lake.descartar()
    get_settings.cache_clear()


def test_tamanho_e_o_dobro_das_activities_com_piso(monkeypatch):
    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "8")
    get_settings.cache_clear()
    assert io_lake._tamanho() == 16

    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "1")
    get_settings.cache_clear()
    assert io_lake._tamanho() == 4  # floor

    monkeypatch.setenv("CFLOW_LAKE_IO_THREADS", "3")
    get_settings.cache_clear()
    assert io_lake._tamanho() == 3  # declared value wins


@pytest.mark.asyncio
async def test_roda_fora_do_executor_default_do_loop():
    """If it fell into the default executor, the separation would not exist - which is the defect
    this module fixes."""
    padrao = await asyncio.to_thread(lambda: threading.current_thread().name)
    lake = await io_lake.em_thread_lake(lambda: threading.current_thread().name)
    assert lake.startswith("cflow-lake")
    assert not padrao.startswith("cflow-lake")


@pytest.mark.asyncio
async def test_devolve_valor_e_propaga_excecao():
    assert await io_lake.em_thread_lake(lambda a, b: a + b, 2, b=3) == 5
    with pytest.raises(ValueError, match="exploded"):
        await io_lake.em_thread_lake(lambda: (_ for _ in ()).throw(ValueError("exploded")))


@pytest.mark.asyncio
async def test_o_pool_atravessa_loops():
    """The pool belongs to the PROCESS: the harness switches event loop on every test, and recreating
    the executor on each switch would bring back the cost of starting threads that the separation
    exists to avoid."""
    primeiro = io_lake._executor()
    assert await io_lake.em_thread_lake(lambda: 1) == 1
    assert io_lake._executor() is primeiro


@pytest.mark.asyncio
async def test_descartar_fecha_e_o_proximo_cria_outro():
    antes = io_lake._executor()
    io_lake.descartar()
    assert io_lake._EXECUTOR is None
    assert await io_lake.em_thread_lake(lambda: 2) == 2
    assert io_lake._executor() is not antes
