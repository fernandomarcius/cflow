"""Per-entity `sistema` flag: only admins see/open/edit/run; Portal and internal chains untouched."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .test_portal import _pagina, _view_pronta

pytestmark = pytest.mark.integration

# (collection in the URL, recurso_tipo in the audit) - all create with {"nome": ...} and return {"id"}.
ENTIDADES = [
    ("pipelines", "pipeline"),
    ("cubos", "cubo"),
    ("views", "view"),
    ("paginas", "pagina"),
    ("funcoes", "biblioteca_funcao"),
]
# The ones with their own listing endpoint (paginas lists via /catalogo, covered separately).
COM_LISTAGEM = [e for e in ENTIDADES if e[0] != "paginas"]


async def _criar(cliente, h, colecao: str, nome: str) -> str:
    r = await cliente.post(f"/{colecao}", json={"nome": nome}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.parametrize("colecao,recurso_tipo", ENTIDADES)
async def test_marcar_audita_e_noop(owner_engine, app_engine, cliente, colecao, recurso_tipo):
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"adm-{colecao}@example.com", perfil="admin"
    )
    rid = await _criar(cliente, adm, colecao, "Item")

    r = await cliente.patch(f"/{colecao}/{rid}/sistema", json={"sistema": True}, headers=adm)
    assert r.status_code == 200 and r.json()["sistema"] is True

    async with owner_engine.connect() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT antes, depois FROM auditoria WHERE acao='editar' "
                        "AND recurso_tipo=:t AND recurso_id=:r"
                    ),
                    {"t": recurso_tipo, "r": rid},
                )
            )
            .mappings()
            .all()
        )
    marca = [x for x in linhas if (x["depois"] or {}).get("sistema") is True]
    assert len(marca) == 1
    assert marca[0]["antes"]["sistema"] is False

    # no-op: marking again with the same value does NOT create a second record
    assert (
        await cliente.patch(f"/{colecao}/{rid}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    async with owner_engine.connect() as conn:
        n = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='editar' AND recurso_tipo=:t "
                    "AND recurso_id=:r AND depois->>'sistema'='true'"
                ),
                {"t": recurso_tipo, "r": rid},
            )
        ).scalar_one()
    assert n == 1


@pytest.mark.parametrize("colecao,recurso_tipo", ENTIDADES)
async def test_especialista_nao_marca(owner_engine, app_engine, cliente, colecao, recurso_tipo):
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"a2-{colecao}@example.com", perfil="admin"
    )
    rid = await _criar(cliente, adm, colecao, "Item2")
    _, esp = await usuario_logado(
        owner_engine, cliente, email=f"e2-{colecao}@example.com", perfil="especialista"
    )
    assert (
        await cliente.patch(f"/{colecao}/{rid}/sistema", json={"sistema": True}, headers=esp)
    ).status_code == 403


@pytest.mark.parametrize("colecao,recurso_tipo", ENTIDADES)
async def test_acesso_direto_negado_ao_especialista(
    owner_engine, app_engine, cliente, colecao, recurso_tipo
):
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"a3-{colecao}@example.com", perfil="admin"
    )
    rid = await _criar(cliente, adm, colecao, "Item3")
    await cliente.patch(f"/{colecao}/{rid}/sistema", json={"sistema": True}, headers=adm)

    _, esp = await usuario_logado(
        owner_engine, cliente, email=f"e3-{colecao}@example.com", perfil="especialista"
    )
    neg = await cliente.get(f"/{colecao}/{rid}", headers=esp)
    assert neg.status_code == 403 and neg.json()["erro"]["codigo"] == "acesso_negado"
    # admin opens it normally
    assert (await cliente.get(f"/{colecao}/{rid}", headers=adm)).status_code == 200


@pytest.mark.parametrize("colecao,recurso_tipo", COM_LISTAGEM)
async def test_listagem_esconde_do_especialista(
    owner_engine, app_engine, cliente, colecao, recurso_tipo
):
    _, adm = await usuario_logado(
        owner_engine, cliente, email=f"a4-{colecao}@example.com", perfil="admin"
    )
    rid = await _criar(cliente, adm, colecao, "Secret")
    await cliente.patch(f"/{colecao}/{rid}/sistema", json={"sistema": True}, headers=adm)

    _, esp = await usuario_logado(
        owner_engine, cliente, email=f"e4-{colecao}@example.com", perfil="especialista"
    )
    ids_esp = [x["id"] for x in (await cliente.get(f"/{colecao}", headers=esp)).json()]
    assert rid not in ids_esp

    itens_adm = (await cliente.get(f"/{colecao}", headers=adm)).json()
    achado = next(x for x in itens_adm if x["id"] == rid)
    assert achado["sistema"] is True


async def test_pipeline_sistema_bloqueia_execucao_studio(owner_engine, app_engine, cliente):
    _, adm = await usuario_logado(owner_engine, cliente, email="a5@example.com", perfil="admin")
    pid = await _criar(cliente, adm, "pipelines", "Refresh")
    await cliente.patch(f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=adm)

    _, esp = await usuario_logado(
        owner_engine, cliente, email="e5@example.com", perfil="especialista"
    )
    r = await cliente.post(f"/pipelines/{pid}/executar", headers=esp)
    assert r.status_code == 403


async def test_portal_e_cadeia_intactos_com_pagina_e_pipeline_sistema(
    owner_engine, app_engine, cliente
):
    """A published page flagged sistema: the operator still sees it in the catalog and can query it; the
    internal chain (view -> cube -> sistema pipeline) resolves. Only DIRECT access to the page is denied (Studio)."""
    _, esp = await usuario_logado(
        owner_engine, cliente, email="espsis@example.com", perfil="especialista"
    )
    vid = await _view_pronta(cliente, esp)
    pag = await _pagina(cliente, esp, vid)

    _, adm = await usuario_logado(owner_engine, cliente, email="admsis@example.com", perfil="admin")
    assert (
        await cliente.patch(f"/paginas/{pag}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200

    _, op = await usuario_logado(
        owner_engine, cliente, email="opsis@example.com", perfil="operador"
    )
    # Portal untouched: the catalog lists the sistema page and the query works
    cat = await cliente.get("/catalogo", headers=op)
    assert cat.status_code == 200 and any(p["id"] == pag for p in cat.json())
    cons = await cliente.post(f"/paginas/{pag}/consultar", json={}, headers=op)
    assert cons.status_code == 200
    assert {x["city"]: x["total"] for x in cons.json()["blocos"][0]["resultado"]["linhas"]} == {
        "SP": 350,
        "RJ": 90,
    }

    # DIRECT access to the page definition (Studio) is denied to non-admins
    assert (await cliente.get(f"/paginas/{pag}", headers=op)).status_code == 403
