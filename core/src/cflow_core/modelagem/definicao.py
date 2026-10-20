"""Validation of cube and view definitions (422 in the standard envelope)."""

from __future__ import annotations

from ..errors import ErroAplicacao


def _erro(msg: str) -> None:
    raise ErroAplicacao(codigo="definicao_invalida", mensagem=msg, status=422)


def validar_cubo(d: dict) -> None:
    fonte = d.get("fonte") or {}
    if not fonte.get("pipeline_id") or not fonte.get("no_id"):
        _erro("A cube needs a source with pipeline_id and no_id.")
    dims = d.get("dimensoes", [])
    meds = d.get("medidas", [])
    if not isinstance(dims, list) or not isinstance(meds, list):
        _erro("dimensoes and medidas must be lists.")
    if not meds:
        _erro("A cube needs at least one named measure.")
    for x in dims:
        if not x.get("nome") or not x.get("coluna"):
            _erro("Each dimension needs a name and a column.")
    for x in meds:
        if not x.get("nome") or not str(x.get("expressao") or "").strip():
            _erro("Each measure needs a name and an expression.")
    nomes_d = [x["nome"] for x in dims]
    nomes_m = [x["nome"] for x in meds]
    if len(set(nomes_d)) != len(nomes_d):
        _erro("Dimension names must be unique.")
    if len(set(nomes_m)) != len(nomes_m):
        _erro("Measure names must be unique.")


def validar_pagina(d: dict) -> None:
    blocos = d.get("blocos", [])
    if not isinstance(blocos, list) or not blocos:
        _erro("A page needs at least one block.")
    if "filtros_pagina" in d and not isinstance(d["filtros_pagina"], list):
        _erro("filtros_pagina must be a list.")
    for b in blocos:
        if b.get("tipo") not in ("pivot", "tabela", "valor"):
            _erro("A block needs a valid type (pivot, tabela or valor).")
        if not b.get("view_id"):
            _erro("Each block references a view (view_id). The page does not compute.")
    # Optional block that connects the page to a pipeline so the operator can run it with
    # parameters. Stored as jsonb (free round-trip); lenient validation: structure only, not
    # the pipeline.
    if "execucao" in d:
        ex = d["execucao"]
        if not isinstance(ex, dict):
            _erro("execucao must be an object.")
        if not ex.get("pipeline_id"):
            _erro("execucao needs a pipeline_id.")
        if "operador" in ex and not isinstance(ex["operador"], list):
            _erro("execucao.operador must be a list.")
        if "derivacoes" in ex and not isinstance(ex["derivacoes"], list):
            _erro("execucao.derivacoes must be a list.")


def validar_view(d: dict) -> None:
    if not d.get("cubo_id"):
        _erro("A view needs a cubo_id.")
    for chave in ("linhas", "colunas", "valores", "filtros", "filtros_liberados"):
        if chave in d and not isinstance(d[chave], list):
            _erro(f"{chave} must be a list.")
    if not d.get("valores"):
        _erro("A view needs at least one value (named measure).")
