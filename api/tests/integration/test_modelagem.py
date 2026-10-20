"""Cube (named measures) + view (pivot), querying the execution snapshot."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


async def _pipeline_executado(cliente, h) -> str:
    """Create, publish and run a pipeline with a city+value output. Returns the pipeline_id."""
    pid = (await cliente.post("/pipelines", json={"nome": "sales-pln"}, headers=h)).json()["id"]
    definicao = {
        "nos": [
            {
                "id": "saida",
                "tipo": "sql",
                "sql": "SELECT * FROM (VALUES ('SP',100),('SP',250),('RJ',90)) t(city, amount)",
            }
        ],
        "arestas": [],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    eid = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, eid, h)  # snapshot ready before the cube is queried
    return pid


async def _cubo(cliente, h, pid) -> str:
    cid = (await cliente.post("/cubos", json={"nome": "Sales"}, headers=h)).json()["id"]
    cubodef = {
        "fonte": {"pipeline_id": pid, "no_id": "saida"},
        "dimensoes": [{"nome": "city", "coluna": "city"}],
        "medidas": [{"nome": "total", "expressao": "sum(amount)"}],
    }
    await cliente.put(
        f"/cubos/{cid}/rascunho", json={"definicao": cubodef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/cubos/{cid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)
    return cid


async def test_cubo_consulta_medida_nomeada(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cb@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, h)
    cid = await _cubo(cliente, h, pid)

    r = await cliente.post(
        f"/cubos/{cid}/consultar", json={"dimensoes": ["city"], "medidas": ["total"]}, headers=h
    )
    assert r.status_code == 200
    rows = {x["city"]: x["total"] for x in r.json()["linhas"]}
    assert rows == {"SP": 350, "RJ": 90}

    # measure outside the cube -> refused (no free-form formula)
    bad = await cliente.post(
        f"/cubos/{cid}/consultar", json={"dimensoes": [], "medidas": ["made_up"]}, headers=h
    )
    assert bad.status_code == 400 and bad.json()["erro"]["codigo"] == "medida_desconhecida"


async def test_cubo_medida_incompativel_com_o_dado_400(owner_engine, app_engine, cliente):
    """A measure the snapshot does not support (e.g. `sum` over text) is a DEFINITION error -> 400 in
    the standard envelope, not 500. The engine detail comes back in `detalhes` so the author can fix it."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cb3@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, h)
    cid = (await cliente.post("/cubos", json={"nome": "Bad measure"}, headers=h)).json()["id"]
    d = {
        "fonte": {"pipeline_id": pid, "no_id": "saida"},
        "dimensoes": [{"nome": "city", "coluna": "city"}],
        "medidas": [{"nome": "sum_text", "expressao": "sum(city)"}],  # city is VARCHAR
    }
    await cliente.put(f"/cubos/{cid}/rascunho", json={"definicao": d, "base_numero": 0}, headers=h)
    await cliente.post(f"/cubos/{cid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)

    r = await cliente.post(
        f"/cubos/{cid}/consultar",
        json={"dimensoes": ["city"], "medidas": ["sum_text"]},
        headers=h,
    )
    assert r.status_code == 400
    erro = r.json()["erro"]
    assert erro["codigo"] == "consulta_invalida"
    assert "measure" in erro["mensagem"]
    assert erro["detalhes"] and erro["detalhes"][0]["campo"] == "definicao"


async def test_cubo_sem_medida_422(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cb2@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, h)
    cid = (await cliente.post("/cubos", json={"nome": "No measure"}, headers=h)).json()["id"]
    d = {
        "fonte": {"pipeline_id": pid, "no_id": "saida"},
        "dimensoes": [{"nome": "city", "coluna": "city"}],
        "medidas": [],
    }
    r = await cliente.put(
        f"/cubos/{cid}/rascunho", json={"definicao": d, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422 and r.json()["erro"]["codigo"] == "definicao_invalida"


async def test_view_pivot_e_filtro_liberado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="vw@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, h)
    cid = await _cubo(cliente, h, pid)

    vid = (await cliente.post("/views", json={"nome": "By city"}, headers=h)).json()["id"]
    viewdef = {
        "cubo_id": cid,
        "linhas": ["city"],
        "valores": ["total"],
        "filtros_liberados": ["city"],
    }
    await cliente.put(
        f"/views/{vid}/rascunho", json={"definicao": viewdef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/views/{vid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)

    r = await cliente.post(f"/views/{vid}/consultar", json={}, headers=h)
    assert {x["city"]: x["total"] for x in r.json()["linhas"]} == {"SP": 350, "RJ": 90}

    # filter on a RELEASED dimension
    r2 = await cliente.post(
        f"/views/{vid}/consultar",
        json={"filtros": [{"dimensao": "city", "op": "igual", "valor": "SP"}]},
        headers=h,
    )
    assert {x["city"]: x["total"] for x in r2.json()["linhas"]} == {"SP": 350}

    # filter on a dimension that is NOT released -> 403
    r3 = await cliente.post(
        f"/views/{vid}/consultar",
        json={"filtros": [{"dimensao": "amount", "op": "igual", "valor": 1}]},
        headers=h,
    )
    assert r3.status_code == 403 and r3.json()["erro"]["codigo"] == "filtro_nao_liberado"


async def test_excluir_cubo_remove_e_audita(owner_engine, app_engine, cliente):
    from sqlalchemy import text

    _, h = await usuario_logado(
        owner_engine, cliente, email="delc@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, h)
    cid = await _cubo(cliente, h, pid)  # creates a published version

    r = await cliente.delete(f"/cubos/{cid}", headers=h)
    assert r.status_code == 204
    assert (await cliente.get(f"/cubos/{cid}", headers=h)).status_code == 404
    assert cid not in [c["id"] for c in (await cliente.get("/cubos", headers=h)).json()]
    async with owner_engine.connect() as conn:
        n_ver = (
            await conn.execute(
                text("SELECT count(*) FROM cubo_versoes WHERE cubo_id = :r"), {"r": cid}
            )
        ).scalar_one()
        n_perm = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM permissoes WHERE recurso_tipo='cubo' AND recurso_id = :r"
                ),
                {"r": cid},
            )
        ).scalar_one()
        n_aud = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='excluir' AND recurso_tipo='cubo' AND recurso_id = :r"
                ),
                {"r": cid},
            )
        ).scalar_one()
    assert n_ver == 0 and n_perm == 0 and n_aud == 1


async def test_excluir_cubo_authz_negada(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="cdono@example.com", perfil="especialista"
    )
    cid = (await cliente.post("/cubos", json={"nome": "Protected"}, headers=dono)).json()["id"]
    _, outro = await usuario_logado(
        owner_engine, cliente, email="cintruso@example.com", perfil="especialista"
    )
    assert (await cliente.delete(f"/cubos/{cid}", headers=outro)).status_code == 403
    assert (await cliente.get(f"/cubos/{cid}", headers=dono)).status_code == 200


async def test_excluir_view_remove_e_audita(owner_engine, app_engine, cliente):
    from sqlalchemy import text

    _, h = await usuario_logado(
        owner_engine, cliente, email="delv@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, h)
    cid = await _cubo(cliente, h, pid)
    vid = (await cliente.post("/views", json={"nome": "To delete"}, headers=h)).json()["id"]
    viewdef = {"cubo_id": cid, "linhas": ["city"], "valores": ["total"]}
    await cliente.put(
        f"/views/{vid}/rascunho", json={"definicao": viewdef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/views/{vid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)

    assert (await cliente.delete(f"/views/{vid}", headers=h)).status_code == 204
    assert (await cliente.get(f"/views/{vid}", headers=h)).status_code == 404
    assert vid not in [v["id"] for v in (await cliente.get("/views", headers=h)).json()]
    async with owner_engine.connect() as conn:
        n_ver = (
            await conn.execute(
                text("SELECT count(*) FROM view_versoes WHERE view_id = :r"), {"r": vid}
            )
        ).scalar_one()
        n_aud = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='excluir' AND recurso_tipo='view' AND recurso_id = :r"
                ),
                {"r": vid},
            )
        ).scalar_one()
    assert n_ver == 0 and n_aud == 1


async def test_excluir_view_authz_negada(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="vdono@example.com", perfil="especialista"
    )
    vid = (await cliente.post("/views", json={"nome": "Protected"}, headers=dono)).json()["id"]
    _, outro = await usuario_logado(
        owner_engine, cliente, email="vintruso@example.com", perfil="especialista"
    )
    assert (await cliente.delete(f"/views/{vid}", headers=outro)).status_code == 403


async def test_operador_consulta_view(owner_engine, app_engine, cliente):
    # especialista cria; operador (Portal) consome — read-only
    _, he = await usuario_logado(
        owner_engine, cliente, email="esp@example.com", perfil="especialista"
    )
    pid = await _pipeline_executado(cliente, he)
    cid = await _cubo(cliente, he, pid)
    vid = (await cliente.post("/views", json={"nome": "Operator"}, headers=he)).json()["id"]
    await cliente.put(
        f"/views/{vid}/rascunho",
        json={
            "definicao": {"cubo_id": cid, "linhas": ["city"], "valores": ["total"]},
            "base_numero": 0,
        },
        headers=he,
    )
    await cliente.post(f"/views/{vid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=he)

    _, ho = await usuario_logado(owner_engine, cliente, email="oper@example.com", perfil="operador")
    r = await cliente.post(f"/views/{vid}/consultar", json={}, headers=ho)
    assert r.status_code == 200 and len(r.json()["linhas"]) == 2
    # the operator does NOT edit cubes/views
    neg = await cliente.post("/cubos", json={"nome": "x"}, headers=ho)
    assert neg.status_code == 403
