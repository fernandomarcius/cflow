"""DuckDB instance PER NODE or SHARED by the process.

Each node used to open its own instance, and each one brings up a catalog, a buffer pool and its
own thread pool (measured on a benchmark host: dozens of live threads at once, and a few ms just to
create and destroy an empty instance, plus the MACROs and input VIEWs that are recreated node by
node).

Sharing ONE instance, a node takes a **slot**: a `cursor()` (its own connection over the same
instance, with its own transaction and `search_path`) and a schema of its own. Measured on DuckDB
1.5.4, which is what makes this safe:

- two cursors with different `search_path` do not see each other (each creates and reads ITS `t`);
- `search_path` does NOT leak to a third cursor: it is per connection, not global;
- a MACRO created in one node's schema does not exist in another's, which keeps libraries PINNED
  per execution isolated from each other (two executions with different versions do not collide);
- 12 threads creating 20 objects each, in their own schemas at the same time: no catalog conflict.

What really changes with the switch: the memory budget becomes ONE (the container's) instead of
one per node, which finally makes "N x limit <= container" hold, and the DuckDB threads become a
single pool. In exchange, a greedy node can make its neighbour fail for lack of memory instead of
failing alone. That is why the switch is a FLAG (`CFLOW_DUCKDB_COMPARTILHADO`, off by default) and
should only go to production after A/B benchmarking and a per-node hash verdict.

The blast radius does NOT change: nodes already run in the same process, so a DuckDB segfault
already took down the whole worker.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

from cflow_core.config import get_settings
from cflow_core.duck import conectar

_LOCK = threading.Lock()
_INSTANCIA = None
_VAGAS: dict[int, asyncio.Semaphore] = {}


class Vaga:
    """One of the permits of `_vagas`, returned by `sair`, which is safe to call from ANY thread.

    `asyncio.Semaphore.release` is not thread-safe, and whoever returns the permit is the node's
    thread (the slot's `fechar` runs alongside the rest of the native work). Hence
    `call_soon_threadsafe`."""

    __slots__ = ("_sem", "_laco", "_ativa")

    def __init__(self, sem, laco) -> None:
        self._sem, self._laco, self._ativa = sem, laco, True

    def sair(self) -> None:
        if not self._ativa:  # idempotent: the slot closes once, but the error path repeats it
            return
        self._ativa = False
        try:
            self._laco.call_soon_threadsafe(self._sem.release)
        except RuntimeError:  # loop already closed (shutdown): nobody to return it to
            pass


class Slot:
    """What a node receives to work with: a connection and, when shared, a schema of its own.

    `con` is used exactly like the previous connection: the node SQL names stay unqualified,
    because the cursor's `search_path` points at the slot's schema."""

    __slots__ = ("con", "_schema", "_vaga")

    def __init__(self, con, schema: str | None, vaga: Vaga | None = None) -> None:
        self.con = con
        self._schema = schema
        self._vaga = vaga

    def fechar(self) -> None:
        """Returns the slot. With an own instance, closes the instance (as it always was); with the
        shared one, drops the node's schema and closes only the cursor: the instance stays alive.

        Runs on the node's thread, alongside the rest of the native work."""
        try:
            if self._schema is not None:
                self.con.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        finally:
            try:
                self.con.close()
            finally:
                if self._vaga is not None:
                    self._vaga.sair()


def _instancia():
    global _INSTANCIA
    with _LOCK:
        if _INSTANCIA is None:
            # `divisor=1`: the budget belongs to the PROCESS, not to a node. With a single
            # instance, dividing by the number of activities would give the whole engine one
            # node's share.
            _INSTANCIA = conectar("execucao", divisor=1)
        return _INSTANCIA


def _vagas() -> asyncio.Semaphore:
    """Cap on DuckDB slots OPEN at the same time IN THIS process, per event loop.

    The budget in `duck.orcamento` divides the container memory by `worker_max_atividades`, which
    is where "N x limit <= container" comes from. But the cap on in-flight nodes is PER EXECUTION
    (`MAX_NOS_EM_PARALELO = 4`), and a chain has dozens of pipelines: measured on benchmark runs,
    **dozens of nodes in flight at once**, hence as many instances, each with the budget computed
    for one of eight. The invariant was violated several times over, and in a container without
    `memory.max` (where `memory_limit` is never even written) each one assumes most of the RAM.

    The semaphore is per LOOP because `asyncio.Semaphore` belongs to the loop awaiting it: the
    test harness swaps loops on every case, and a global semaphore would hang on the second.
    """
    laco = asyncio.get_running_loop()
    sem = _VAGAS.get(id(laco))
    if sem is None:
        _VAGAS.clear()  # previous loops are dead; the dict must not grow
        sem = asyncio.Semaphore(max(1, int(get_settings().worker_max_atividades)))
        _VAGAS[id(laco)] = sem
    return sem


async def entrar() -> Vaga:
    """Waits for a permit. On the LOOP, before going to the thread to open the instance, which is
    what makes the cap hold without blocking an executor thread just to wait.

    No path between this wait and the return awaits ANOTHER node: in the source branch, the
    `VOO_EXTRACAO` is awaited BEFORE the instance is born, so whoever waits for a sibling does not
    yet hold a permit. If that changes, the ordering must be rechecked: it is the form of deadlock
    this cap can create."""
    laco = asyncio.get_running_loop()
    sem = _vagas()
    await sem.acquire()
    return Vaga(sem, laco)


def abrir(perfil: str = "execucao", vaga: Vaga | None = None) -> Slot:
    if not get_settings().duckdb_compartilhado:
        return Slot(conectar(perfil), None, vaga)
    schema = f"no_{uuid.uuid4().hex}"
    con = _instancia().cursor()
    try:
        con.execute(f'CREATE SCHEMA "{schema}"')
        con.execute(f"SET search_path='{schema}'")
    except BaseException:
        con.close()
        raise
    return Slot(con, schema, vaga)


def cursor(con):
    """Child cursor ALREADY in the parent's schema.

    A cursor is another CONNECTION on the same instance and is born with the default `search_path`.
    With the shared instance the node's table lives in its own schema and the child would not see
    it: partitioned publishing, which opens one cursor per scope, failed with `Table with name ...
    does not exist!` pointing at the right schema in the "did you mean". With an own instance the
    `search_path` is already the default and the `SET` is a no-op."""
    caminho = con.execute("SELECT current_setting('search_path')").fetchone()[0]
    filho = con.cursor()
    try:
        if caminho:
            filho.execute(f"SET search_path='{caminho}'")
    except BaseException:
        filho.close()
        raise
    return filho


def descartar() -> None:
    """Closes the shared instance (tests and worker shutdown). The next `abrir` creates another."""
    global _INSTANCIA
    with _LOCK:
        if _INSTANCIA is not None:
            _INSTANCIA.close()
            _INSTANCIA = None
