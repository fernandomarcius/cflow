"""Executor DEDICATED to lake object I/O.

The worker replaces the loop's default executor with a `ThreadPoolExecutor` of
`CFLOW_WORKER_MAX_ATIVIDADES` threads (`worker/main.py`); that is what gives real parallelism to
CPU-bound nodes (DuckDB, sandbox, `COPY`, content hashing). Lake I/O used the SAME pool: writing
a 2 KB manifest queued behind a `COPY` of a million rows.

The profiles are opposite. A DuckDB thread wants a CPU slot and holds it; a lake thread releases
the GIL and waits on disk (or network, on GCS). In the same pool, I/O waiting consumes CPU slots,
and a partitioned publication takes six trips to the pool (data, manifest, pointer, immutable
index, index read, current index).

Measured in a benchmark beforehand: output nodes consume a disproportionate share of node time
to move a small share of the rows, and a node publishing ZERO rows costs far more under
concurrency than without it. It is queueing, not work.

The pool belongs to the PROCESS, not the loop: a `ThreadPoolExecutor` is safe across loops and
tests switch loops on every case. `descartar` exists for tests and for shutdown.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .config import get_settings

_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None


def _tamanho() -> int:
    """Threads of the lake pool. Twice the activities, because a thread here spends its life
    BLOCKED on disk or network: it does not compete for CPU, and a tight pool only makes it wait
    twice. Floor of 4 for the case `worker_max_atividades` is 1."""
    s = get_settings()
    declarado = int(s.lake_io_threads or 0)
    return declarado if declarado > 0 else max(4, 2 * int(s.worker_max_atividades))


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=_tamanho(), thread_name_prefix="cflow-lake")
            atexit.register(descartar)
        return _EXECUTOR


async def em_thread_lake[T](fn: Callable[..., T], /, *args, **kwargs) -> T:
    """Like `asyncio.to_thread`, but on the lake pool. Same rule as `execucao.bloqueante`: nothing
    inside may touch `transacao()`/asyncpg, which belong to the loop."""
    laco = asyncio.get_running_loop()
    return await laco.run_in_executor(_executor(), functools.partial(fn, *args, **kwargs))


def descartar() -> None:
    """Closes the pool. The next `em_thread_lake` creates another."""
    global _EXECUTOR
    with _LOCK:
        executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=False)
