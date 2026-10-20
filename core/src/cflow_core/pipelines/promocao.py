"""Pipeline promotion between environments: the PURE part, no I/O.

Promoting a pipeline is not copying the JSON: the definition references `conexao_id` and
`pipeline_id`, and those ids belong to ONE database. Carrying them over as-is points the
promoted pipeline at nothing, or worse, at the wrong resource that happens to have that id in
the target.

What matches the two sides is the **NAME**. This module holds the functions that find the
references and rewrite them; reading and writing (over HTTP or directly against the database)
is left out because that is what differs between the two paths.

The rewrite is the SAME in both directions: export swaps id -> name, import swaps name -> id.
It is one function with an inverted map; two implementations would diverge the day a new kind
of reference appeared, and the divergence would only show up in production.
"""

from __future__ import annotations

import copy
import re
import unicodedata


def refs_pipeline(definicao: dict) -> list[str]:
    """Ids (or names) of pipelines referenced by source nodes with `origem: pipeline`."""
    vistos: list[str] = []
    for no in definicao.get("nos", []):
        pid = (no.get("config") or {}).get("pipeline_id")
        if pid and str(pid) not in vistos:
            vistos.append(str(pid))
    return vistos


def refs_conexao(definicao: dict) -> list[str]:
    """Ids (or names) of connections referenced by any node."""
    vistos: list[str] = []
    for no in definicao.get("nos", []):
        cid = (no.get("config") or {}).get("conexao_id")
        if cid and str(cid) not in vistos:
            vistos.append(str(cid))
    return vistos


def refs_biblioteca(definicao: dict) -> list[str]:
    """Ids (or names) of the function libraries declared at the top of the definition."""
    return [str(b) for b in (definicao.get("bibliotecas") or [])]


def slug(nome: str) -> str:
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s or "pipeline"


class ReferenciaAusente(Exception):
    """A reference in the definition has no counterpart on the other side. Names the node and the
    target: promoting with a broken reference yields a pipeline that runs and returns nothing."""


def reescrever(
    definicao: dict,
    *,
    mapa_conexoes: dict[str, str],
    mapa_pipelines: dict[str, str],
    caminho_saida: str | None = None,
    caminho_por_no: dict[str, str] | None = None,
    nome_pipeline: str = "",
    mapa_bibliotecas: dict[str, str] | None = None,
) -> tuple[dict, list[str]]:
    """Re-points connection/pipeline (and, if requested, the write destination).

    Returns (definition, notes). The maps are generic from/to maps: `{id -> name}` on export,
    `{name -> id}` on import.
    """
    d = copy.deepcopy(definicao)
    caminho_por_no = caminho_por_no or {}
    notas: list[str] = []

    # The MACRO library is also referenced by ID, and the id CHANGES between environments.
    # Promoting without remapping carries an id that does not exist on the other side, and the
    # pipeline only breaks at EXECUTION, with a "Scalar Function ... does not exist" error;
    # validation passes.
    if mapa_bibliotecas is not None and d.get("bibliotecas"):
        novas = []
        for b in d["bibliotecas"]:
            alvo = mapa_bibliotecas.get(str(b))
            if alvo is None:
                raise ReferenciaAusente(
                    f"function library {b} has no counterpart on the other side"
                )
            if alvo != str(b):
                notas.append(f"library {b} -> {alvo}")
            novas.append(alvo)
        d["bibliotecas"] = novas
    for no in d.get("nos", []):
        cfg = no.get("config")
        if not isinstance(cfg, dict):
            continue
        no_id = str(no.get("id"))

        cid = cfg.get("conexao_id")
        if cid is not None:
            destino = mapa_conexoes.get(str(cid))
            if destino is None:
                raise ReferenciaAusente(
                    f"node '{no_id}': connection {cid} has no counterpart on the other side"
                )
            if destino != str(cid):
                notas.append(f"{no_id}: conexao_id {cid} -> {destino}")
            cfg["conexao_id"] = destino

        pid = cfg.get("pipeline_id")
        if pid is not None:
            destino = mapa_pipelines.get(str(pid))
            if destino is None:
                raise ReferenciaAusente(
                    f"node '{no_id}': pipeline {pid} has no counterpart on the other side"
                )
            if destino != str(pid):
                notas.append(f"{no_id}: pipeline_id {pid} -> {destino}")
            cfg["pipeline_id"] = destino

        if no.get("tipo") == "saida" and (caminho_saida or caminho_por_no):
            # The map key is the OUTPUT's `nome` (the dataset), not the node id: in a closure of
            # pipelines every terminal node is called "saida", so the id collides, the dataset
            # does not.
            dataset = str(cfg.get("nome") or slug(nome_pipeline))
            novo = caminho_por_no.get(dataset) or caminho_por_no.get(no_id)
            if novo is None and caminho_saida:
                novo = f"{caminho_saida.rstrip('/')}/{dataset}"
            if novo and cfg.get("caminho") != novo:
                notas.append(f"output {dataset}: path {cfg.get('caminho')!r} -> {novo!r}")
                cfg["caminho"] = novo
    return d, notas


class CicloDePipelines(Exception):
    """Cyclic dependency between pipelines. Impossible by construction (publishing validates the
    DAG), but the error names both sides instead of overflowing the stack."""


def ordem_topologica(deps_por_id: dict[str, list[str]], raizes: list[str]) -> list[str]:
    """Ids in import order: **dependency before dependent**.

    Not the order the roots were requested in, nor a breadth-first order: the promotion file is
    applied top to bottom, and a root that appears before the pipeline it reads fails on the
    target with "pipeline has no counterpart", after already having created the earlier ones.
    """
    ordem: list[str] = []
    pronto: set[str] = set()
    visitando: set[str] = set()

    def visitar(pid: str, caminho: list[str]) -> None:
        if pid in pronto:
            return
        if pid in visitando:
            raise CicloDePipelines(" -> ".join([*caminho, pid]))
        visitando.add(pid)
        for dep in deps_por_id.get(pid, []):
            visitar(dep, [*caminho, pid])
        visitando.discard(pid)
        pronto.add(pid)
        ordem.append(pid)  # after its deps

    for r in raizes:
        visitar(r, [])
    return ordem
