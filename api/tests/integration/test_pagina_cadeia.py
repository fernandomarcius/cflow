"""Page block whose cube reads a pipeline OTHER than the executed one (root DAG + upstream).

Motivating scenario: two pages sharing the cubes of the "auxiliary tables". The page runs the
root, and a query using the root's `execucao_id` found no execution of the auxiliary pipeline ->
`sem_execucao`, even though both pipelines showed as "completed" in the list.

Resolution is now by the dispatch CHAIN (`execucoes.cadeia_id`): /executar with dependencies runs
root + upstreams under a single cadeia_id, and the other pipeline's block reads the SIBLING
execution of that same dispatch (`origem.resolucao == "cadeia"`). With no sibling in the chain it
falls back to the pipeline's latest completed execution and SAYS so (`origem.resolucao == "ultima"`).
"""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


async def _publicar_pipeline(cliente, h, nome: str, definicao: dict) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def _view_de(cliente, h, pipe_id: str, no_id: str, coluna: str, nome: str) -> str:
    cid = (await cliente.post("/cubos", json={"nome": f"C {nome}"}, headers=h)).json()["id"]
    cubodef = {
        "fonte": {"pipeline_id": pipe_id, "no_id": no_id},
        "dimensoes": [{"nome": coluna, "coluna": coluna}],
        "medidas": [{"nome": "qtd", "expressao": "count(*)"}],
    }
    await cliente.put(
        f"/cubos/{cid}/rascunho", json={"definicao": cubodef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/cubos/{cid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)
    vid = (await cliente.post("/views", json={"nome": f"V {nome}"}, headers=h)).json()["id"]
    viewdef = {"cubo_id": cid, "linhas": [coluna], "valores": ["qtd"], "filtros_liberados": []}
    await cliente.put(
        f"/views/{vid}/rascunho", json={"definicao": viewdef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/views/{vid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)
    return vid


async def _cenario(cliente, h) -> dict:
    """AUX (auxiliary pipeline) + RAIZ (pipeline-type source on AUX) + a page with one block of each."""
    aux = await _publicar_pipeline(
        cliente,
        h,
        "auxiliary",
        {
            "nos": [
                {"id": "tab", "tipo": "sql", "sql": "SELECT * FROM (VALUES ('a'),('b')) t(letra)"}
            ],
            "arestas": [],
        },
    )
    raiz = await _publicar_pipeline(
        cliente,
        h,
        "root",
        {
            "nos": [
                {
                    "id": "src",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": aux, "no_id": "tab"},
                },
                {
                    "id": "conta",
                    "tipo": "sql",
                    "sql": "SELECT count(*) AS n, 'x' AS grupo FROM src",
                },
            ],
            "arestas": [{"de": "src", "para": "conta"}],
        },
    )
    v_raiz = await _view_de(cliente, h, raiz, "conta", "grupo", "raiz")
    v_aux = await _view_de(cliente, h, aux, "tab", "letra", "aux")
    pag = (await cliente.post("/paginas", json={"nome": "Two sources"}, headers=h)).json()["id"]
    paginadef = {
        "blocos": [
            {"tipo": "pivot", "view_id": v_raiz, "titulo": "From root"},
            {"tipo": "pivot", "view_id": v_aux, "titulo": "From auxiliary"},
        ],
        "filtros_pagina": [],
    }
    await cliente.put(
        f"/paginas/{pag}/rascunho", json={"definicao": paginadef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/paginas/{pag}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)
    return {"aux": aux, "raiz": raiz, "pagina": pag}


async def test_bloco_de_outro_pipeline_resolve_pela_cadeia(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cadeia1@example.com", perfil="especialista"
    )
    c = await _cenario(cliente, h)
    # Run the ROOT (the DAG runs AUX first, same dispatch/cadeia_id).
    r = await cliente.post(f"/pipelines/{c['raiz']}/executar", headers=h)
    assert r.status_code == 200, r.text
    exec_raiz = r.json()["id"]
    await aguardar_execucao(cliente, exec_raiz, h)

    # Query the page with the root execution: the AUX block resolves via the chain (no 400).
    r = await cliente.post(
        f"/paginas/{c['pagina']}/consultar", json={"execucao_id": exec_raiz}, headers=h
    )
    assert r.status_code == 200, r.text
    blocos = {b["titulo"]: b for b in r.json()["blocos"]}
    origem_raiz = blocos["From root"]["resultado"]["origem"]
    origem_aux = blocos["From auxiliary"]["resultado"]["origem"]
    assert origem_raiz["execucao_id"] == exec_raiz
    assert origem_raiz["resolucao"] == "execucao"
    # The auxiliary block came from ANOTHER execution: the sibling of the SAME dispatch, and it says how.
    assert origem_aux["execucao_id"] != exec_raiz
    assert origem_aux["resolucao"] == "cadeia"
    assert blocos["From auxiliary"]["resultado"]["linhas"]  # data present ('a','b')

    # Both executions share the chain (the "same instant" criterion).
    from sqlalchemy import text

    async with app_engine.connect() as conn:
        cadeias = (
            (
                await conn.execute(
                    text("SELECT DISTINCT cadeia_id FROM execucoes WHERE id IN (:a, :b)"),
                    {"a": exec_raiz, "b": origem_aux["execucao_id"]},
                )
            )
            .scalars()
            .all()
        )
    assert len(cadeias) == 1 and cadeias[0] is not None


async def test_bloco_sem_irma_na_cadeia_cai_na_ultima_e_avisa(owner_engine, app_engine, cliente):
    """Execution WITHOUT a chain containing the cube's pipeline (undeclared dependency): falls back
    to the block pipeline's latest completed execution, marked `resolucao == "ultima"`, and never
    the 400 `sem_execucao` with both pipelines completed."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cadeia2@example.com", perfil="especialista"
    )
    # Two INDEPENDENT pipelines (no edge between them).
    solo = await _publicar_pipeline(
        cliente,
        h,
        "solo",
        {"nos": [{"id": "s", "tipo": "sql", "sql": "SELECT 1 AS um"}], "arestas": []},
    )
    outro = await _publicar_pipeline(
        cliente,
        h,
        "other",
        {"nos": [{"id": "o", "tipo": "sql", "sql": "SELECT 2 AS dois"}], "arestas": []},
    )
    v_solo = await _view_de(cliente, h, solo, "s", "um", "solo")
    v_outro = await _view_de(cliente, h, outro, "o", "dois", "other")
    pag = (await cliente.post("/paginas", json={"nome": "Mixed"}, headers=h)).json()["id"]
    paginadef = {
        "blocos": [
            {"tipo": "pivot", "view_id": v_solo, "titulo": "Solo"},
            {"tipo": "pivot", "view_id": v_outro, "titulo": "Outro"},
        ],
        "filtros_pagina": [],
    }
    await cliente.put(
        f"/paginas/{pag}/rascunho", json={"definicao": paginadef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/paginas/{pag}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)

    e_outro = (await cliente.post(f"/pipelines/{outro}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, e_outro, h)
    e_solo = (await cliente.post(f"/pipelines/{solo}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, e_solo, h)

    r = await cliente.post(f"/paginas/{pag}/consultar", json={"execucao_id": e_solo}, headers=h)
    assert r.status_code == 200, r.text
    blocos = {b["titulo"]: b for b in r.json()["blocos"]}
    assert blocos["Solo"]["resultado"]["origem"]["resolucao"] == "execucao"
    o = blocos["Outro"]["resultado"]["origem"]
    assert o["resolucao"] == "ultima"  # outside the dispatch, and the origin SAYS so
    assert o["execucao_id"] == e_outro


async def test_bloco_de_outro_pipeline_sem_execucao_nenhuma_segue_400(
    owner_engine, app_engine, cliente
):
    """The fallback invents no data: a block pipeline that NEVER ran stays `sem_execucao`."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cadeia3@example.com", perfil="especialista"
    )
    a = await _publicar_pipeline(
        cliente,
        h,
        "a-exec",
        {"nos": [{"id": "s", "tipo": "sql", "sql": "SELECT 1 AS um"}], "arestas": []},
    )
    nunca = await _publicar_pipeline(
        cliente,
        h,
        "never-ran",
        {"nos": [{"id": "o", "tipo": "sql", "sql": "SELECT 2 AS dois"}], "arestas": []},
    )
    v_nunca = await _view_de(cliente, h, nunca, "o", "dois", "nunca")
    pag = (await cliente.post("/paginas", json={"nome": "Never"}, headers=h)).json()["id"]
    await cliente.put(
        f"/paginas/{pag}/rascunho",
        json={
            "definicao": {
                "blocos": [{"tipo": "pivot", "view_id": v_nunca, "titulo": "Never"}],
                "filtros_pagina": [],
            },
            "base_numero": 0,
        },
        headers=h,
    )
    await cliente.post(f"/paginas/{pag}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)
    e_a = (await cliente.post(f"/pipelines/{a}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, e_a, h)

    r = await cliente.post(f"/paginas/{pag}/consultar", json={"execucao_id": e_a}, headers=h)
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "sem_execucao"
