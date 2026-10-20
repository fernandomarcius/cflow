"""Pipeline DAG: chaining A -> B when B has a `pipeline` source.

The `origem:"pipeline"` sources of a pipeline ARE ALREADY its dependencies; there is no separate
declaration, the dependency graph is DERIVED from the JSON. `resolver_ordem` takes the transitive
closure over pipelines (not nodes) and returns the topological order (root last), pinning the
PUBLISHED VERSION of each UPSTREAM; for the root, whatever the caller asked for applies (with no
number, the latest: that is the Studio Run button testing the draft). A cycle between pipelines
is an explicit error.

Whoever orchestrates (engine/worker) runs in that order and passes the `{pipeline_id ->
execution_id}` map (freshness) along, so B's pipeline source reads the EXACT snapshot of the
execution of A made now.

Besides the declared edges there is an IMPLICIT order among pipelines ALREADY in the closure: a
pipeline that reads a dataset through `origem:"publicado"` runs after the one that publishes it
in the same chain (see `resolver_ordem`). It orders, never adds a pipeline: `publicado` remains
the read that does NOT re-run the producer.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ErroAplicacao
from . import service as ps


def dependencias_diretas(definicao: dict) -> list[dict]:
    """Pipelines THIS one depends on directly: the source nodes with `origem:"pipeline"`.

    Returns `[{pipeline_id, no_id, versao}]` (versao may be None = latest published). Ignores
    connection sources and other nodes. No dependencies -> empty list.
    """
    deps: list[dict] = []
    for no in definicao.get("nos", []):
        if no.get("tipo") != "fonte":
            continue
        cfg = no.get("config") or {}
        if (cfg.get("origem") or "conexao") == "pipeline" and cfg.get("pipeline_id"):
            deps.append(
                {
                    "pipeline_id": str(cfg["pipeline_id"]),
                    "no_id": cfg.get("no_id"),
                    "versao": cfg.get("versao"),
                }
            )
    return deps


def leituras_publicadas(definicao: dict) -> set[tuple[str, str]]:
    """`(dataset, layer)` that this pipeline reads through an `origem:"publicado"` source.

    A missing layer means `gold`, the SAME default as `ingestao.leitura.resolver_publicado` and
    the source validation: resolving here with another default would match the reader with the
    wrong producer.
    """
    lidos: set[tuple[str, str]] = set()
    for no in definicao.get("nos", []):
        cfg = no.get("config") or {}
        if no.get("tipo") == "fonte" and cfg.get("origem") == "publicado" and cfg.get("dataset"):
            lidos.add((str(cfg["dataset"]), str(cfg.get("camada") or "gold")))
    return lidos


def publicacoes_lake(definicao: dict) -> set[tuple[str, str]]:
    """`(dataset, layer)` that this pipeline publishes in an `saida` node with `destino: lake`.

    A missing layer means `silver`, the default of the `lake` destination. `iceberg` is left
    out: `origem:"publicado"` reads the lake pointer, not the catalog.
    """
    publicados: set[tuple[str, str]] = set()
    for no in definicao.get("nos", []):
        cfg = no.get("config") or {}
        destino = str(cfg.get("destino") or "arquivo").lower()
        if no.get("tipo") == "saida" and destino == "lake" and cfg.get("dataset"):
            publicados.add((str(cfg["dataset"]), str(cfg.get("camada") or "silver")))
    return publicados


def _alcanca(adj: dict[str, set[str]], origem: str, alvo: str) -> bool:
    """Does `origem` depend (transitively) on `alvo`? Search over the in-memory `adj`."""
    pilha, vistos = [origem], {origem}
    while pilha:
        atual = pilha.pop()
        if atual == alvo:
            return True
        for prox in adj.get(atual, ()):
            if prox not in vistos:
                vistos.add(prox)
                pilha.append(prox)
    return False


def _ordem_implicita(ordem: list[dict], adj: dict[str, set[str]]) -> bool:
    """Add to `adj` the IMPLICIT dependency `publicado` reader -> chain producer.

    Only among those already in `ordem` (the definitions of the versions the chain will run). An
    edge that would close a cycle is discarded: the explicit one wins, and a cycle is only an
    error when someone declared it. Walks in the order of `ordem` so that the discard, when there
    is one, is the same on every call. Returns whether any edge was added.
    """
    produtores: dict[tuple[str, str], list[str]] = {}
    for item in ordem:
        for publicado in sorted(publicacoes_lake(item["definicao"])):
            produtores.setdefault(publicado, []).append(item["pipeline_id"])
    mudou = False
    for item in ordem:
        leitor = item["pipeline_id"]
        for lido in sorted(leituras_publicadas(item["definicao"])):
            for produtor in produtores.get(lido, []):
                if produtor == leitor or produtor in adj[leitor]:
                    continue
                if _alcanca(adj, produtor, leitor):
                    continue
                adj[leitor].add(produtor)
                mudou = True
    return mudou


def arestas_da_cadeia(itens: list[dict], *, raiz: str | None = None) -> dict[str, dict[str, str]]:
    """Dependencies among the pipelines of a run that ALREADY EXECUTED, rebuilt from the definitions.

    The `deps` computed at dispatch (`resolver_ordem`) only travel in the workflow payload; they
    are not stored. To draw the map of a past run they are DERIVED again with the SAME rules:
    explicit (`dependencias_diretas`) and implicit (`_ordem_implicita`, `publicado` reader ->
    chain producer). `itens` is `[{pipeline_id, definicao}]` with the definition of the VERSION
    THAT RAN; today's may read something else.

    Returns `{pipeline_id: {dependency: kind}}`, `kind` in `pipeline` | `publicado`. A dependency
    outside `itens` is ignored (the map only links what is in the run). The discard of an
    implicit edge that would close a cycle depends on visit order; it mimics the dispatch (from
    `raiz`, depth-first), and only in a pathological case could the discard differ from what the
    run used.
    """
    defs = {str(i["pipeline_id"]): i.get("definicao") or {} for i in itens}
    adj: dict[str, set[str]] = {pid: set() for pid in defs}
    for pid, definicao in defs.items():
        for d in dependencias_diretas(definicao):
            if d["pipeline_id"] in defs and d["pipeline_id"] != pid:
                adj[pid].add(d["pipeline_id"])
    explicitas = {pid: set(deps) for pid, deps in adj.items()}

    # Dependency before dependent (post-order), starting at the root like `resolver_ordem`.
    ordem: list[dict] = []
    visitados: set[str] = set()

    def visitar(pid: str, pilha: set[str]) -> None:
        if (
            pid in visitados or pid in pilha
        ):  # `pilha` only guards against a cycle in corrupted data
            return
        pilha.add(pid)
        for dep in sorted(explicitas[pid]):
            visitar(dep, pilha)
        pilha.discard(pid)
        visitados.add(pid)
        ordem.append({"pipeline_id": pid, "definicao": defs[pid]})

    for pid in ([raiz] if raiz in defs else []) + sorted(defs):
        visitar(pid, set())
    _ordem_implicita(ordem, adj)
    return {
        pid: {dep: ("pipeline" if dep in explicitas[pid] else "publicado") for dep in sorted(deps)}
        for pid, deps in adj.items()
    }


def restringir_arestas(
    arestas: dict[str, dict[str, str]], visiveis: set[str]
) -> dict[str, dict[str, str]]:
    """Edges only among `visiveis`, BRIDGING whoever was left out.

    Non-admins do not see `sistema` pipelines; dropping the node and its edges would leave the
    neighbours looking independent, and the map would lie about who waits for whom. The
    dependency crosses the hidden node: it is `pipeline` if some path is entirely `pipeline`
    sources, otherwise `publicado`.
    """
    saida: dict[str, dict[str, str]] = {}
    for pid in sorted(visiveis):
        deps: dict[str, str] = {}
        pilha = [(d, t) for d, t in arestas.get(pid, {}).items()]
        vistos: set[str] = set()
        while pilha:
            dep, tipo = pilha.pop()
            if dep in visiveis:
                if deps.get(dep) != "pipeline":
                    deps[dep] = tipo
                continue
            if dep in vistos:
                continue
            vistos.add(dep)
            for d2, t2 in arestas.get(dep, {}).items():
                pilha.append((d2, "pipeline" if tipo == t2 == "pipeline" else "publicado"))
        saida[pid] = dict(sorted(deps.items()))
    return saida


async def resolver_ordem(
    conn: AsyncConnection, raiz_id: uuid.UUID | str, versao_numero: int | None = None
) -> list[dict]:
    """Transitive closure + topological order of the pipelines to run to produce the root fresh.

    Returns `[{pipeline_id, versao_numero, definicao, nivel, deps}]` in execution order
    (dependencies first, root last). Each pipeline appears ONCE; the UPSTREAM version is pinned
    (the one requested on the edge or, without it, the PUBLISHED one, never the draft); the
    ROOT's is whatever the caller asked for in `versao_numero` (None = the latest, the Studio Run
    behaviour). `nivel` (0 = no dependencies) marks what may run in PARALLEL: pipelines at the
    SAME level are siblings (none depends on the other); whoever dispatches the chain groups by
    level. Errors: `sem_definicao` (upstream with no published version) and `ciclo_pipelines`
    (cyclic dependency between pipelines).

    **Implicit order through `origem:"publicado"`.** If pipeline R in the closure reads
    `(dataset, camada)` through a `publicado` source and another pipeline W in the SAME closure
    publishes that `(dataset, camada)` in an `saida` node with `destino: lake` (in the version
    the chain will run), R runs at a level AFTER W. Without this, readers at level 0 would read
    the pointer of the PREVIOUS run while the producer of their own chain had not even started:
    two as-of points in the same run, and the run green. Three deliberate limits:

    - **it orders, it does not drag.** No pipeline joins the closure because of this; with no
      producer in the chain, the level is the usual one and the reader reads whatever pointer
      exists (reading without re-running the producer is why `publicado` exists);
    - **an implicit edge that would create a cycle is ignored**, never a 400. A cycle is an error
      only when EXPLICIT (someone declared both sources as `pipeline`);
    - **it ignores partitions.** Reading another partition of the same dataset also waits for the
      producer: it costs parallelism, never correctness.

    When any implicit edge is added, the list is re-sorted (stable) by level so that whoever
    walks it sequentially still sees dependency before dependent; with none, the list is exactly
    what it was before.
    """
    raiz_id = str(raiz_id)
    # cache of the definition/version resolved per pipeline + DAG edges
    info: dict[str, dict] = {}  # pid -> {pipeline_id, versao_numero, definicao}
    adj: dict[str, set[str]] = {}  # pid -> {deps}
    EM_VISITA, PRONTO = 1, 2
    estado: dict[str, int] = {}
    ordem: list[dict] = []

    async def carregar(pid: str, versao: int | None, *, raiz: bool) -> dict:
        if versao is None and not raiz:
            # UPSTREAM with no version requested on the edge -> the PUBLISHED one, never "the
            # latest". `ps.obter(pid, None)` returns the latest number OF ANY KIND, drafts
            # included: a half-finished draft left open in the Studio would enter the run of
            # whoever only triggered the consumer, who is not even looking at the upstream. For
            # the ROOT "latest version" still applies: that is the Studio Run button, which
            # exists to test before publishing.
            versao = await ps.numero_publicado(conn, uuid.UUID(pid))
            if not versao:
                raise ErroAplicacao(
                    codigo="sem_definicao",
                    mensagem="Chain pipeline has no published version to run.",
                    status=400,
                )
        det = await ps.obter(conn, uuid.UUID(pid), versao)
        if det["numero"] == 0:
            raise ErroAplicacao(
                codigo="sem_definicao",
                mensagem="Chain pipeline has no published version to run.",
                status=400,
            )
        return {"pipeline_id": pid, "versao_numero": det["numero"], "definicao": det["definicao"]}

    async def visitar(pid: str, versao: int | None, *, raiz: bool = False) -> None:
        if estado.get(pid) == PRONTO:
            return
        if estado.get(pid) == EM_VISITA:
            raise ErroAplicacao(
                codigo="ciclo_pipelines",
                mensagem="Cyclic dependency between pipelines in the chain.",
                status=400,
            )
        estado[pid] = EM_VISITA
        if pid not in info:
            info[pid] = await carregar(pid, versao, raiz=raiz)
        adj[pid] = set()
        for d in dependencias_diretas(info[pid]["definicao"]):
            dep_pid = d["pipeline_id"]
            adj[pid].add(dep_pid)
            await visitar(dep_pid, d.get("versao"))
        estado[pid] = PRONTO
        ordem.append(info[pid])

    await visitar(raiz_id, versao_numero, raiz=True)
    implicita = _ordem_implicita(ordem, adj)

    # Level = 1 + max(level of deps); no deps = 0. Pure (only over the in-memory `adj`).
    nivel_cache: dict[str, int] = {}

    def _nivel(pid: str) -> int:
        if pid in nivel_cache:
            return nivel_cache[pid]
        deps = adj.get(pid, set())
        n = 1 + max((_nivel(d) for d in deps), default=-1)
        nivel_cache[pid] = n
        return n

    for item in ordem:
        item["nivel"] = _nivel(item["pipeline_id"])
        # The DIRECT dependencies, declared and implicit. Dispatch decides when each pipeline
        # starts by them (`execucao.paralelo`), not by level: the level groups what does not
        # depend on each other but makes the next level wait for pipelines it does not even read.
        item["deps"] = sorted(adj.get(item["pipeline_id"], set()))
    if implicita:
        # Every dependent -> dependency pair has a strictly greater level on the dependent, so
        # the stable sort by level is topological for both kinds of edge, and the root, which
        # depends on all, stays last.
        ordem.sort(key=lambda it: it["nivel"])
    return ordem
