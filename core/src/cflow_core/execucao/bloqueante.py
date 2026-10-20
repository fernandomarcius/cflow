"""Blocking call outside the worker's event loop.

Activities are `async def` and run on the process loop; inside them, DuckDB (synchronous) and the
sandboxes (`subprocess.run`) would hold the whole loop while working. With
`max_concurrent_activities=8` the worker APPEARED to run eight nodes, but one CPU-bound node froze
the other seven, along with the SDK's poll/heartbeat. Measured: real parallelism of a heavy node is
about 1 per process. `em_thread` sends the call to the loop's executor (a `ThreadPoolExecutor` with
`CFLOW_WORKER_MAX_ATIVIDADES` threads, see `worker/main.py`); DuckDB releases the GIL during a
query, so the threads really scale.

Rules: nothing inside the function running in a thread may touch `transacao()`/asyncpg (they belong
to the loop); the DuckDB connection of ONE node is used by one thread at a time (sequential, never
concurrent).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def em_thread[T](fn: Callable[..., T], /, *args, **kwargs) -> T:
    return await asyncio.to_thread(fn, *args, **kwargs)
