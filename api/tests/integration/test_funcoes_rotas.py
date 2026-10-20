"""Function library HTTP routes: versioned CRUD (mirrors the cube) + `/avaliar` (tests a macro
against sample values in an ephemeral DuckDB). Editing requires specialist/admin."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

# A simple macro: twice(x) = (CAST(x AS INTEGER) * 2). Valid in preview and in execution.
_DEF_TWICE = {"funcoes": [{"nome": "twice", "args": ["x"], "corpo": "(CAST(x AS INTEGER) * 2)"}]}


async def _criar_publicar(cliente, h, nome="Macros") -> str:
    """Create the library, save a draft with `twice` and publish. Returns the biblioteca_id."""
    bid = (await cliente.post("/funcoes", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/funcoes/{bid}/rascunho", json={"definicao": _DEF_TWICE, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 1
    p = await cliente.post(
        f"/funcoes/{bid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert p.status_code == 200, p.text
    return bid


async def test_crud_publicar_obter_renomear(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="bf1@example.com", perfil="especialista"
    )

    # POST creates and returns {id} (same semantics as the cube: 200 with body {id}, not 201).
    r = await cliente.post("/funcoes", json={"nome": "Macros", "descricao": "useful"}, headers=h)
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    assert bid

    bid2 = await _criar_publicar(cliente, h, nome="Others")  # reuses the full flow

    # GET returns the published version (definition + number)
    det = (await cliente.get(f"/funcoes/{bid2}", headers=h)).json()
    assert det["numero"] == 2  # 1 draft + 1 publication
    assert det["definicao"] == _DEF_TWICE

    # the listing contains both
    ids = [x["id"] for x in (await cliente.get("/funcoes", headers=h)).json()]
    assert bid in ids and bid2 in ids

    # rename (the owner administers)
    rr = await cliente.patch(f"/funcoes/{bid2}", json={"nome": "Renamed"}, headers=h)
    assert rr.status_code == 200 and rr.json()["nome"] == "Renamed"


async def test_authz_criar_negado_para_operador(owner_engine, app_engine, cliente):
    _, ho = await usuario_logado(owner_engine, cliente, email="bfop@example.com", perfil="operador")
    r = await cliente.post("/funcoes", json={"nome": "x"}, headers=ho)
    assert r.status_code == 403 and r.json()["erro"]["codigo"] == "nao_autorizado"


async def test_authz_editar_negado_para_nao_dono(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="bfdono@example.com", perfil="especialista"
    )
    bid = (await cliente.post("/funcoes", json={"nome": "Protected"}, headers=dono)).json()["id"]

    # another specialist (not the owner, not admin) -> 403 on edit/delete
    _, outro = await usuario_logado(
        owner_engine, cliente, email="bfoutro@example.com", perfil="especialista"
    )
    neg = await cliente.put(
        f"/funcoes/{bid}/rascunho", json={"definicao": _DEF_TWICE, "base_numero": 0}, headers=outro
    )
    assert neg.status_code == 403 and neg.json()["erro"]["codigo"] == "acesso_negado"
    assert (await cliente.delete(f"/funcoes/{bid}", headers=outro)).status_code == 403

    # admin passes (administers everything)
    _, adm = await usuario_logado(owner_engine, cliente, email="bfadm@example.com", perfil="admin")
    ok = await cliente.put(
        f"/funcoes/{bid}/rascunho", json={"definicao": _DEF_TWICE, "base_numero": 0}, headers=adm
    )
    assert ok.status_code == 200


async def test_rascunho_definicao_invalida_422(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="bfinv@example.com", perfil="especialista"
    )
    bid = (await cliente.post("/funcoes", json={"nome": "Invalid"}, headers=h)).json()["id"]

    # empty funcoes -> 422
    r = await cliente.put(
        f"/funcoes/{bid}/rascunho", json={"definicao": {"funcoes": []}, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422 and r.json()["erro"]["codigo"] == "definicao_invalida"

    # invalid function name -> 422
    ruim = {"funcoes": [{"nome": "1not-valid", "args": ["x"], "corpo": "x"}]}
    r2 = await cliente.put(
        f"/funcoes/{bid}/rascunho", json={"definicao": ruim, "base_numero": 0}, headers=h
    )
    assert r2.status_code == 422 and r2.json()["erro"]["codigo"] == "definicao_invalida"


async def test_avaliar_macro_publicada(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="bfav@example.com", perfil="especialista"
    )
    bid = await _criar_publicar(cliente, h)

    # no definicao in the body -> uses the published version
    r = await cliente.post(f"/funcoes/{bid}/avaliar", json={"chamada": "twice(21)"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["resultado"] == "42"


async def test_avaliar_com_definicao_no_corpo(owner_engine, app_engine, cliente):
    # definicao in the body (editing preview) overrides the saved one; library still has no version
    _, h = await usuario_logado(
        owner_engine, cliente, email="bfavd@example.com", perfil="especialista"
    )
    bid = (await cliente.post("/funcoes", json={"nome": "Preview"}, headers=h)).json()["id"]
    r = await cliente.post(
        f"/funcoes/{bid}/avaliar",
        json={"chamada": "twice(5)", "definicao": _DEF_TWICE},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["resultado"] == "10"


async def test_avaliar_expressao_quebrada_400(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="bfavx@example.com", perfil="especialista"
    )
    bid = await _criar_publicar(cliente, h)

    # missing function / invalid expression -> sanitized 400 (no stack)
    r = await cliente.post(
        f"/funcoes/{bid}/avaliar", json={"chamada": "does_not_exist(1)"}, headers=h
    )
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "avaliacao_falha"
    assert "Failed to evaluate the function" in r.json()["erro"]["mensagem"]


async def test_avaliar_negado_para_operador(owner_engine, app_engine, cliente):
    _, he = await usuario_logado(
        owner_engine, cliente, email="bfave@example.com", perfil="especialista"
    )
    bid = await _criar_publicar(cliente, he)
    _, ho = await usuario_logado(
        owner_engine, cliente, email="bfavo@example.com", perfil="operador"
    )
    r = await cliente.post(f"/funcoes/{bid}/avaliar", json={"chamada": "twice(1)"}, headers=ho)
    assert r.status_code == 403


async def test_auditoria_criar_e_publicar(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="bfaud@example.com", perfil="especialista"
    )
    bid = await _criar_publicar(cliente, h)

    async with owner_engine.connect() as conn:
        n_criar = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='criar' AND recurso_tipo='biblioteca_funcao' AND recurso_id = :r"
                ),
                {"r": bid},
            )
        ).scalar_one()
        n_pub = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='publicar' AND recurso_tipo='biblioteca_funcao' AND recurso_id = :r"
                ),
                {"r": bid},
            )
        ).scalar_one()
    assert n_criar == 1 and n_pub == 1
