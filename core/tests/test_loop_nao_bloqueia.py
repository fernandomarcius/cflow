"""Heavy node work runs off the event loop.

A synchronous `duck.execute(...)` inside the `async def` activity would freeze the whole loop: the
other `max_concurrent_activities` slots and the SDK heartbeat would wait for the node to finish. The
proof is about LATENCY: while a heavy SQL runs in a thread, an `asyncio.sleep(0)` on the same loop
must return within milliseconds."""

from __future__ import annotations

import asyncio
import time

from cflow_core.duck import conectar
from cflow_core.execucao.bloqueante import em_thread


async def test_sql_pesado_em_thread_nao_congela_o_loop():
    duck = conectar("execucao", divisor=1)
    duck.execute("SET threads = 1")
    latencias: list[float] = []
    parar = False

    async def _mede():
        while not parar:
            t0 = time.perf_counter()
            await asyncio.sleep(0)
            latencias.append(time.perf_counter() - t0)
            await asyncio.sleep(0.005)

    medidor = asyncio.create_task(_mede())
    t0 = time.perf_counter()
    await em_thread(
        duck.execute,
        "CREATE TABLE t AS SELECT range AS i, md5(CAST(range AS VARCHAR)) AS h FROM range(3000000)",
    )
    duracao = time.perf_counter() - t0
    parar = True
    await medidor
    assert duracao > 0.05, "the SQL had to be heavy enough for the proof to hold"
    assert len(latencias) > 3
    latencias.sort()
    p95 = latencias[int(len(latencias) * 0.95)]
    assert p95 < 0.05, f"loop blocked: p95 {p95:.3f}s"


async def test_bloqueante_devolve_o_valor_e_propaga_excecao():
    assert await em_thread(lambda x: x * 2, 21) == 42

    def _quebra():
        raise ValueError("from inside the thread")

    try:
        await em_thread(_quebra)
    except ValueError as e:
        assert "from inside" in str(e)
    else:  # pragma: no cover
        raise AssertionError("exception did not propagate")
