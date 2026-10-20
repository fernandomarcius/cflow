"""Fine-grained per-resource permission: the creator administers; another specialist cannot edit;
admin passes."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

DEF = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}


async def test_pipeline_so_o_dono_edita(owner_engine, app_engine, cliente):
    _, ha = await usuario_logado(
        owner_engine, cliente, email="owner@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "p"}, headers=ha)).json()["id"]

    # another specialist (no grant) -> generic 403
    _, hb = await usuario_logado(
        owner_engine, cliente, email="other@example.com", perfil="especialista"
    )
    neg = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 0}, headers=hb
    )
    assert neg.status_code == 403 and neg.json()["erro"]["codigo"] == "acesso_negado"

    # the owner edits
    ok = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 0}, headers=ha
    )
    assert ok.status_code == 200

    # admin always passes (administers everything)
    _, hadm = await usuario_logado(owner_engine, cliente, email="adm@example.com", perfil="admin")
    radm = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 1}, headers=hadm
    )
    assert radm.status_code == 200


async def test_cubo_so_o_dono_edita(owner_engine, app_engine, cliente):
    _, ha = await usuario_logado(
        owner_engine, cliente, email="ownerc@example.com", perfil="especialista"
    )
    cid = (await cliente.post("/cubos", json={"nome": "c"}, headers=ha)).json()["id"]
    cubodef = {
        "fonte": {"pipeline_id": "00000000-0000-0000-0000-000000000000", "no_id": "x"},
        "dimensoes": [],
        "medidas": [{"nome": "n", "expressao": "count(*)"}],
    }
    _, hb = await usuario_logado(
        owner_engine, cliente, email="otherc@example.com", perfil="especialista"
    )
    neg = await cliente.put(
        f"/cubos/{cid}/rascunho", json={"definicao": cubodef, "base_numero": 0}, headers=hb
    )
    assert neg.status_code == 403
    ok = await cliente.put(
        f"/cubos/{cid}/rascunho", json={"definicao": cubodef, "base_numero": 0}, headers=ha
    )
    assert ok.status_code == 200
