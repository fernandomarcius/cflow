"""Dependency-driven execution: each item starts when ITS dependencies have completed.

Serves both orchestration levels with the same function:

- **Pipelines of a chain** (`ExecutarDAG` and the inline chained run). A level-by-level run with a
  barrier made level n+1 wait for ALL of level n, even for items that did not depend on the slow
  ones. Here an item waits only for its own dependencies.
- **Nodes of a pipeline** (`ExecutarPipeline`). A single activity at a time in topological order
  made independent sources add up instead of overlapping.

**What does not change.** The order is still the existing topological one
(`dag.resolver_ordem`, `definicao.ordem_topologica`); what goes away is waiting for non
dependencies. No item runs before ALL its dependencies completed, and whoever reads a snapshot
still reads the one of the exact run. Failure follows the same rule as the per-level loop: after
the first failure **nothing new starts**, what is in flight finishes (otherwise the run would stay
'running' forever with nobody waiting), and the first failure, in list order, is the one raised.

**Effect nodes run one at a time, in the usual order** (`TIPOS_SERIAIS`). Two independent
`escrita_sql` nodes may have their relative order as an implicit contract (write table A before B),
and the external database cannot undo it. The previous topological order applies between them;
parallelizing an external effect is a business decision, not a performance one.

**Deterministic in Temporal.** The function runs INSIDE the workflow (`ExecutarDAG`,
`ExecutarPipeline`), where every decision must repeat identically on replay. So it uses neither
`asyncio.wait` (returns a `set`, whose iteration order is not stable across processes) nor clocks:
each item is a coroutine created in list order that awaits its dependencies' futures, and the
Temporal event loop wakes waiters in registration order.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping

#: Nodes that write outside the platform (or pull from an external origin): among themselves they
#: run one at a time in topological order. Mirrors `reexecucao.TIPOS_EFEITO` without importing it:
#: this module is loaded inside the Temporal workflow and must not pull in the engine.
TIPOS_SERIAIS = frozenset({"escrita_sql", "http", "email", "ingestao"})

#: Cap on nodes of ONE pipeline running at the same time. The activity budget belongs to the WORKER
#: (`CFLOW_WORKER_MAX_ATIVIDADES`, default 8) and is shared among ALL in-flight runs; without a cap,
#: a wide pipeline would take every slot and the other runs of the chain would queue. Half the
#: default leaves the other half for siblings. It is a CONSTANT, not a setting: it drives the
#: workflow schedule, and reading it from the environment would break determinism on a replay with
#: another value. Changing it is new code, with a new `workflow.patched` marker.
#:
#: Raising it to 8 was tried and MEASURED: it is WORSE. On a 4-core host the remaining idle time is
#: not parked slots but the real limit of cores with the GIL and subprocesses competing; more
#: overlap trades throughput for contention. Do not retry without changing the core count.
MAX_NOS_EM_PARALELO = 4

#: Separate cap for nodes that only publish to the lake (`saida` with `destino: lake`). Publishing
#: to a local lake is an `os.link`, but on object storage it is network wait, so these nodes do not
#: compete for heavy DuckDB work or the GIL: they get their own slots, while CPU-bound nodes stay
#: limited to `MAX_NOS_EM_PARALELO`. A constant for the same reason as the cap above.
MAX_NOS_IO_EM_PARALELO = 12


def so_publica(no: dict) -> bool:
    """Is the node an output that only publishes to the lake (network wait, not CPU)?"""
    return (
        no.get("tipo") == "saida"
        and str((no.get("config") or {}).get("destino") or "arquivo").lower() == "lake"
    )


def serial(no: dict) -> bool:
    """Does the node join the one-at-a-time queue? External effects always; and outputs that do NOT
    go to the lake (`arquivo`, `iceberg`): two of them may target the same path or table, and in
    series the last one always wins, while in parallel any could. The `lake` output is parallel:
    validation refuses two nodes publishing the same dataset in a graph, and the dataset index is
    written under a lock."""
    tipo = no.get("tipo")
    if tipo in TIPOS_SERIAIS:
        return True
    if tipo == "saida":
        return str((no.get("config") or {}).get("destino") or "arquivo").lower() != "lake"
    return False


def _conferir(ordem: list[str], deps: Mapping[str, Iterable[str]]) -> None:
    """Every dependency in the list comes BEFORE its dependent. Without this an inverted edge (or a
    cycle) would hang the run waiting for a future that never resolves."""
    posicao = {item: i for i, item in enumerate(ordem)}
    if len(posicao) != len(ordem):
        raise ValueError("Repeated item in the execution order.")
    for item in ordem:
        for dep in deps.get(item, ()):
            if dep in posicao and posicao[dep] >= posicao[item]:
                raise ValueError(
                    f"Dependency '{dep}' of '{item}' does not come before it in the execution order."
                )


async def rodar_por_dependencia(
    ordem: list[str],
    deps: Mapping[str, Iterable[str]],
    rodar: Callable[[str], Awaitable[object]],
    *,
    limite: int | None = None,
    io: Callable[[str], bool] | None = None,
    limite_io: int | None = None,
) -> None:
    """Run `rodar(item)` for each item of `ordem`, each as soon as its dependencies have
    completed successfully.

    `io` + `limite_io`: items for which `io(item)` is true compete for their OWN cap (`limite_io`)
    instead of `limite`. Without both, every item uses `limite`.

    `ordem` is topological. A dependency outside `ordem` counts as already satisfied (re-run from a
    node: the upstream entered as completed before the trigger). After the first failure no new
    item starts; in-flight ones finish, and then the first failure (in list order) is raised, the
    same semantics as the per-level `gather(..., return_exceptions=True)` this replaces.

    `limite`: at most that many items in `rodar` at once (ready items wait for a slot, in list
    order). Without a limit, every ready item starts.
    """
    _conferir(ordem, deps)
    laco = asyncio.get_running_loop()
    concluiu = {item: laco.create_future() for item in ordem}
    parar = False
    vagas = asyncio.Semaphore(limite) if limite else None
    vagas_io = asyncio.Semaphore(limite_io) if (io is not None and limite_io) else None

    async def _um(item: str) -> None:
        nonlocal parar
        for dep in deps.get(item, ()):
            if dep in concluiu and not await concluiu[dep]:
                concluiu[item].set_result(False)
                return
        vaga = vagas_io if (vagas_io is not None and io(item)) else vagas
        async with vaga if vaga is not None else contextlib.nullcontext():
            # Checked AFTER getting the slot: the failure may have happened while waiting.
            if parar:
                concluiu[item].set_result(False)
                return
            try:
                await rodar(item)
            except BaseException:
                parar = True
                concluiu[item].set_result(False)
                raise
        concluiu[item].set_result(True)

    resultados = await asyncio.gather(*(_um(item) for item in ordem), return_exceptions=True)
    falhas = [r for r in resultados if isinstance(r, BaseException)]
    if falhas:
        raise falhas[0]


def grupo_serial(no: dict) -> str | None:
    """Serial chain of the node (`config.grupo_serial`); None = the single usual queue. Only
    non-empty text counts: anything else is refused by validation (`definicao.validar`) and here,
    for safety, falls into the single queue instead of becoming a group named `"['b1']"`."""
    g = (no.get("config") or {}).get("grupo_serial")
    return g.strip() if isinstance(g, str) and g.strip() else None


def declara_grupo_serial(definicao: dict) -> bool:
    """Does any node of the definition declare `config.grupo_serial`? Decides, in the workflow,
    whether the group's `workflow.patched` marker enters the history; a pipeline without groups
    does not record it."""
    return any(grupo_serial(n) for n in definicao.get("nos") or [])


def dependencias_dos_nos(
    definicao: dict, ordem: list[str], *, grupos: bool = True
) -> dict[str, list[str]]:
    """Dependencies of each node in `ordem`: the graph edges, plus the queue of `serial` nodes
    (external effects and outputs outside the lake), which still run one at a time in topological
    order.

    With `grupos`, there is ONE queue per `config.grupo_serial`, plus the single queue of nodes
    without a group: different groups do not wait for each other; order holds inside a group. A
    serial node WITHOUT a group fans in (waits for the last of each group) and the groups restart
    after it, which makes it a BARRIER if it falls in the middle of the topological order between
    grouped nodes. `grupos=False` ignores the declaration and reproduces the single queue: it is
    the replay path for workflows started before the `grupo-serial` marker.

    Without a declared group both paths are identical. Only nodes present in `ordem` are used: the
    list may be PARTIAL (`desde_no` re-run), and the upstream left out is already completed. Pure."""
    presentes = set(ordem)
    deps: dict[str, set[str]] = {item: set() for item in ordem}
    for aresta in definicao.get("arestas") or []:
        de, para = aresta.get("de"), aresta.get("para")
        if de in presentes and para in presentes:
            deps[para].add(de)
    nos = {n.get("id"): n for n in definicao.get("nos") or []}
    ultimo_sem_grupo: str | None = None
    ultimo_do_grupo: dict[str, str] = {}
    for item in ordem:
        no = nos.get(item) or {}
        if not serial(no):
            continue
        grupo = grupo_serial(no) if grupos else None
        if grupo is None:
            # Single queue: waits for the previous ungrouped node AND the last of each open group (fan-in).
            if ultimo_sem_grupo is not None:
                deps[item].add(ultimo_sem_grupo)
            deps[item].update(ultimo_do_grupo.values())
            ultimo_sem_grupo = item
            ultimo_do_grupo = {}
        else:
            # Group queue: waits for the previous node OF THE GROUP; without it, the last ungrouped
            # node (which closed the groups before); with neither, only the edges.
            anterior = ultimo_do_grupo.get(grupo, ultimo_sem_grupo)
            if anterior is not None:
                deps[item].add(anterior)
            ultimo_do_grupo[grupo] = item
    posicao = {item: i for i, item in enumerate(ordem)}
    return {item: sorted(v, key=posicao.__getitem__) for item, v in deps.items()}
