"""`GET /views?resumo=true` and `GET /cubos?resumo=true`: the state of each list item in one trip.

It proves three things:
1. the summary is the SAME one the front end used to assemble over `GET /{collection}/{id}` +
   `/versoes` per item (and `GET /catalogo` + `GET /paginas/{id}` per page), so the screen does not change;
2. the listing runs a FIXED number of queries, whatever the number of items (no N+1), and a single
   query reads the versions;
3. visibility equal to the listing's: a non-admin neither sees nor counts a system item.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


@contextlib.contextmanager
def _consultas():
    """Record every SQL command executed in the process (any engine) while the block runs."""
    vistos: list[str] = []

    def _antes(conn, cursor, statement, parameters, context, executemany):
        vistos.append(statement)

    event.listen(Engine, "before_cursor_execute", _antes)
    try:
        yield vistos
    finally:
        event.remove(Engine, "before_cursor_execute", _antes)


async def _criar(cliente, h, colecao: str, nome: str) -> str:
    r = await cliente.post(f"/{colecao}", json={"nome": nome}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _rascunho(cliente, h, colecao: str, rid: str, definicao: dict, base: int) -> None:
    r = await cliente.put(
        f"/{colecao}/{rid}/rascunho", json={"definicao": definicao, "base_numero": base}, headers=h
    )
    assert r.status_code == 200, r.text


async def _publicar(cliente, h, colecao: str, rid: str, base: int) -> None:
    r = await cliente.post(f"/{colecao}/{rid}/publicar", json={"base_numero": base}, headers=h)
    assert r.status_code == 200, r.text


def _cubodef(dims: int, meds: int, pipeline: str = "p-1", no: str = "saida") -> dict:
    return {
        "fonte": {"pipeline_id": pipeline, "no_id": no},
        "dimensoes": [{"nome": f"d{i}", "coluna": f"d{i}"} for i in range(dims)],
        "medidas": [{"nome": f"m{i}", "expressao": "sum(x)"} for i in range(meds)],
    }


def _viewdef(cubo_id: str, linhas: int, colunas: int, valores: int, liberados: int) -> dict:
    return {
        "cubo_id": cubo_id,
        "linhas": [f"l{i}" for i in range(linhas)],
        "colunas": [f"c{i}" for i in range(colunas)],
        "valores": [f"v{i}" for i in range(valores)],
        "filtros_liberados": [f"f{i}" for i in range(liberados)],
    }


async def _pagina(cliente, h, nome: str, vids: list[str], *, publicar: bool) -> str:
    pid = await _criar(cliente, h, "paginas", nome)
    definicao = {"blocos": [{"tipo": "pivot", "view_id": v} for v in vids]}
    await _rascunho(cliente, h, "paginas", pid, definicao, 0)
    if publicar:
        await _publicar(cliente, h, "paginas", pid, 1)
    return pid


# --- What the front end assembled, item by item (View.tsx / Cubo.tsx before the summary) ---------


def _n(x) -> int:
    return len(x) if isinstance(x, list) else 0


def _publicada_de(versoes: list[dict]) -> dict | None:
    pub = next((v for v in versoes if v["ativa"]), None)
    return {"numero": pub["numero"], "criado_em": pub["criado_em"]} if pub else None


async def _view_como_o_front_via(cliente, h, vid: str) -> dict:
    det = (await cliente.get(f"/views/{vid}", headers=h)).json()
    versoes = (await cliente.get(f"/views/{vid}/versoes", headers=h)).json()
    d = det["definicao"] or {}
    return {
        "cubo_id": str(d["cubo_id"]) if d.get("cubo_id") else None,
        "linhas": _n(d.get("linhas")),
        "colunas": _n(d.get("colunas")),
        "valores": _n(d.get("valores")),
        "filtros_liberados": _n(d.get("filtros_liberados")),
        "ultimo": versoes[0]["numero"] if versoes else 0,
        "publicada": _publicada_de(versoes),
    }


async def _uso_em_paginas_como_o_front_via(cliente, h) -> tuple[dict[str, int], int]:
    mapa: dict[str, int] = {}
    ocultas = 0
    for p in (await cliente.get("/catalogo", headers=h)).json():
        r = await cliente.get(f"/paginas/{p['id']}", headers=h)
        if r.status_code == 403:
            ocultas += 1
            continue
        assert r.status_code == 200, r.text
        blocos = (r.json().get("definicao") or {}).get("blocos") or []
        for v in {str(b.get("view_id") or b.get("vid") or "") for b in blocos} - {""}:
            mapa[v] = mapa.get(v, 0) + 1
    return mapa, ocultas


async def _cubo_como_o_front_via(cliente, h, cid: str) -> dict:
    det = (await cliente.get(f"/cubos/{cid}", headers=h)).json()
    versoes = (await cliente.get(f"/cubos/{cid}/versoes", headers=h)).json()
    d = det["definicao"] or {}
    fonte = d.get("fonte") or {}
    return {
        "dimensoes": _n(d.get("dimensoes")),
        "medidas": _n(d.get("medidas")),
        "pipeline_id": str(fonte["pipeline_id"]) if fonte.get("pipeline_id") else None,
        "no_id": str(fonte["no_id"]) if fonte.get("no_id") else None,
        "ultimo": versoes[0]["numero"] if versoes else 0,
        "publicada": _publicada_de(versoes),
    }


async def _dependentes_como_o_front_via(cliente, h) -> dict[str, int]:
    mapa: dict[str, int] = {}
    for v in (await cliente.get("/views", headers=h)).json():
        d = (await cliente.get(f"/views/{v['id']}", headers=h)).json()["definicao"] or {}
        if d.get("cubo_id"):
            mapa[str(d["cubo_id"])] = mapa.get(str(d["cubo_id"]), 0) + 1
    return mapa


# --- Scenario --------------------------------------------------------------------------------


async def _cenario(owner_engine, cliente, adm: dict, esp: dict) -> dict:
    """Cubes and views in different states, published and unpublished pages, and system items."""
    c1 = await _criar(cliente, esp, "cubos", "C1")
    await _rascunho(cliente, esp, "cubos", c1, _cubodef(2, 1), 0)
    await _publicar(cliente, esp, "cubos", c1, 1)  # publicada = 2
    await _rascunho(cliente, esp, "cubos", c1, _cubodef(3, 2, "p-2", "other"), 2)  # 3 à frente
    c2 = await _criar(cliente, esp, "cubos", "C2 no version")
    c3 = await _criar(cliente, esp, "cubos", "C3 draft only")
    await _rascunho(cliente, esp, "cubos", c3, _cubodef(1, 1), 0)

    v1 = await _criar(cliente, esp, "views", "V1")
    await _rascunho(cliente, esp, "views", v1, _viewdef(c3, 2, 1, 1, 1), 0)
    await _publicar(cliente, esp, "views", v1, 1)
    # The latest version swaps the cube: it is the one that counts (what the screen reopens to edit).
    await _rascunho(cliente, esp, "views", v1, _viewdef(c1, 1, 2, 3, 0), 2)
    v2 = await _criar(cliente, esp, "views", "V2 no version")
    v3 = await _criar(cliente, esp, "views", "V3")
    await _rascunho(cliente, esp, "views", v3, _viewdef(c1, 1, 0, 1, 2), 0)
    await _publicar(cliente, esp, "views", v3, 1)

    # V1 twice on the same page counts 1; an unpublished page does not enter the catalog.
    await _pagina(cliente, esp, "P1", [v1, v1, v3], publicar=True)
    await _pagina(cliente, esp, "P2 draft", [v1], publicar=False)
    # Published with V3; the draft ahead now displays V1 (the LATEST version counts, as in the front end).
    # Its block stores the view in the legacy field `vid` (today's API only writes `view_id`, hence
    # the direct UPDATE): the front end read both, and so does the summary.
    p3 = await _pagina(cliente, esp, "P3", [v3], publicar=True)
    await _rascunho(cliente, esp, "paginas", p3, {"blocos": [{"tipo": "pivot", "view_id": v1}]}, 2)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE pagina_versoes SET definicao = jsonb_build_object('blocos', "
                "jsonb_build_array(jsonb_build_object('tipo', 'pivot', 'vid', CAST(:v AS text)))) "
                "WHERE pagina_id = :p AND numero = 3"
            ),
            {"v": v1, "p": p3},
        )

    # System items: only the admin sees them, and only for the admin do they enter the counts.
    vs = await _criar(cliente, adm, "views", "V system")
    await _rascunho(cliente, adm, "views", vs, _viewdef(c1, 1, 0, 1, 0), 0)
    assert (
        await cliente.patch(f"/views/{vs}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    cs = await _criar(cliente, adm, "cubos", "C system")
    assert (
        await cliente.patch(f"/cubos/{cs}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    ps = await _pagina(cliente, adm, "P system", [v1], publicar=True)
    assert (
        await cliente.patch(f"/paginas/{ps}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    return {"c1": c1, "c2": c2, "c3": c3, "cs": cs, "v1": v1, "v2": v2, "v3": v3, "vs": vs}


async def _logados(owner_engine, cliente) -> tuple[dict, dict]:
    sufixo = uuid.uuid4().hex[:6]
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"adm-res-{sufixo}@example.com", perfil="admin"
    )
    _, esp = await usuario_logado(
        owner_engine, cliente, email=f"esp-res-{sufixo}@example.com", perfil="especialista"
    )
    return adm, esp


@pytest.mark.parametrize("perfil", ["especialista", "admin"])
async def test_resumo_das_views_e_o_que_o_front_montava(owner_engine, app_engine, cliente, perfil):
    adm, esp = await _logados(owner_engine, cliente)
    ids = await _cenario(owner_engine, cliente, adm, esp)
    h = adm if perfil == "admin" else esp

    r = await cliente.get("/views?resumo=true", headers=h)
    assert r.status_code == 200, r.text
    itens = r.json()
    simples = (await cliente.get("/views", headers=h)).json()
    # Same items, same order, same usual fields: the summary only adds.
    assert [{k: i[k] for k in ("id", "nome", "sistema")} for i in itens] == simples

    uso, ocultas = await _uso_em_paginas_como_o_front_via(cliente, h)
    for item in itens:
        esperado = await _view_como_o_front_via(cliente, h, item["id"])
        esperado["paginas"] = uso.get(item["id"], 0)
        esperado["paginas_nao_verificadas"] = ocultas
        assert item["resumo"] == esperado, item["nome"]

    por_id = {i["id"]: i["resumo"] for i in itens}
    # Explicit anchors (not just "same as the other path"): the scenario says what it must yield.
    v1 = por_id[ids["v1"]]
    assert (v1["cubo_id"], v1["linhas"], v1["colunas"], v1["valores"]) == (ids["c1"], 1, 2, 3)
    assert v1["ultimo"] == 3 and v1["publicada"]["numero"] == 2
    assert por_id[ids["v2"]] == {
        "cubo_id": None,
        "linhas": 0,
        "colunas": 0,
        "valores": 0,
        "filtros_liberados": 0,
        "ultimo": 0,
        "publicada": None,
        "paginas": 0,
        "paginas_nao_verificadas": 0 if perfil == "admin" else 1,
    }
    # V1: P1 (twice, counts 1) + P3 (by the latest version) [+ system P, admin only].
    assert v1["paginas"] == (3 if perfil == "admin" else 2)
    assert por_id[ids["v3"]]["paginas"] == 1  # P1; P3 stopped displaying it in the latest version
    assert (ids["vs"] in por_id) is (perfil == "admin")


@pytest.mark.parametrize("perfil", ["especialista", "admin"])
async def test_resumo_dos_cubos_e_o_que_o_front_montava(owner_engine, app_engine, cliente, perfil):
    adm, esp = await _logados(owner_engine, cliente)
    ids = await _cenario(owner_engine, cliente, adm, esp)
    h = adm if perfil == "admin" else esp

    r = await cliente.get("/cubos?resumo=true", headers=h)
    assert r.status_code == 200, r.text
    itens = r.json()
    simples = (await cliente.get("/cubos", headers=h)).json()
    assert [{k: i[k] for k in ("id", "nome", "sistema")} for i in itens] == simples

    dependentes = await _dependentes_como_o_front_via(cliente, h)
    for item in itens:
        esperado = await _cubo_como_o_front_via(cliente, h, item["id"])
        esperado["views"] = dependentes.get(item["id"], 0)
        assert item["resumo"] == esperado, item["nome"]

    por_id = {i["id"]: i["resumo"] for i in itens}
    c1 = por_id[ids["c1"]]
    assert (c1["dimensoes"], c1["medidas"], c1["pipeline_id"], c1["no_id"]) == (
        3,
        2,
        "p-2",
        "other",
    )
    assert c1["ultimo"] == 3 and c1["publicada"]["numero"] == 2
    # V1 (latest version) + V3 [+ system V, admin only]; C3 lost V1 in the latest version.
    assert c1["views"] == (3 if perfil == "admin" else 2)
    assert por_id[ids["c3"]]["views"] == 0 and por_id[ids["c3"]]["publicada"] is None
    assert por_id[ids["c2"]]["ultimo"] == 0 and por_id[ids["c2"]]["pipeline_id"] is None
    assert (ids["cs"] in por_id) is (perfil == "admin")


async def test_sem_o_parametro_a_resposta_e_a_de_sempre(owner_engine, app_engine, cliente):
    adm, esp = await _logados(owner_engine, cliente)
    await _cenario(owner_engine, cliente, adm, esp)
    for colecao in ("views", "cubos"):
        itens = (await cliente.get(f"/{colecao}", headers=esp)).json()
        assert itens and all(set(i) == {"id", "nome", "sistema"} for i in itens), colecao
        falso = (await cliente.get(f"/{colecao}?resumo=false", headers=esp)).json()
        assert falso == itens


@pytest.mark.parametrize("colecao,versoes", [("views", "view_versoes"), ("cubos", "cubo_versoes")])
async def test_numero_de_consultas_nao_cresce_com_a_lista(
    owner_engine, app_engine, cliente, colecao, versoes
):
    """No N+1: the SAME number of SQL commands with 1 item and with 8, and only one reads the versions."""
    adm, esp = await _logados(owner_engine, cliente)
    await _cenario(owner_engine, cliente, adm, esp)

    async def medir() -> list[str]:
        with _consultas() as vistos:
            r = await cliente.get(f"/{colecao}?resumo=true", headers=esp)
        assert r.status_code == 200, r.text
        return vistos

    poucos = await medir()
    for i in range(8):
        rid = await _criar(cliente, esp, colecao, f"extra {i}")
        definicao = _viewdef("c-x", 1, 1, 1, 1) if colecao == "views" else _cubodef(1, 1)
        await _rascunho(cliente, esp, colecao, rid, definicao, 0)
    muitos = await medir()

    assert len(muitos) == len(poucos), (len(poucos), len(muitos), muitos)
    assert len([s for s in muitos if versoes in s]) == 1, muitos


async def test_cubos_com_resumo_segue_restrito_a_especialista(owner_engine, app_engine, cliente):
    _, op = await usuario_logado(
        owner_engine, cliente, email="op-res@example.com", perfil="operador"
    )
    assert (await cliente.get("/cubos?resumo=true", headers=op)).status_code == 403
    # `GET /views` is open to any logged-in user (the Portal consumes it), with the summary too.
    assert (await cliente.get("/views?resumo=true", headers=op)).status_code == 200
    assert (await cliente.get("/views?resumo=true")).status_code == 401
