"""Sample of ONE node's snapshot (`GET /execucoes/{id}/nos/{no}/preview`): how many rows come back
(`?limite=`) and who may see the RAW DATA of an intermediate node.

The guard here is narrower than the one for the execution STATUS: following an execution
(`GET /execucoes/{id}`) is open to whoever triggered it, including the operator who clicked
"Generate report" in the Portal. Seeing a node's raw data is not: the snapshot went through no
named measure or page filters, so it is Studio-only (specialist/admin) and never for a pipeline
marked `sistema`.
"""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration

# 120 rows: above the default (50) and the UI's 2nd step (100), below the cap (200).
DEFINICAO = {
    "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT n FROM range(120) t(n)"}],
    "arestas": [],
}


async def _pipeline_executado(cliente, h) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": "prev-no"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": DEFINICAO, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/pipelines/{pid}/publicar", json={"base_numero": 1}, headers=h)
    r = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()
    res = await aguardar_execucao(cliente, r["id"], h)
    assert res["status"] == "concluida", res
    return res["id"]


async def test_limite_da_amostra(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="prev@example.com", perfil="especialista"
    )
    eid = await _pipeline_executado(cliente, h)

    async def amostra(qs: str = "") -> dict:
        r = await cliente.get(f"/execucoes/{eid}/nos/n1/preview{qs}", headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    # Default: 50 rows. `contagem` is always the snapshot total, not the sample's: that is what lets
    # the UI say "50 of 120 row(s)".
    padrao = await amostra()
    assert len(padrao["amostra"]) == 50 and padrao["contagem"] == 120
    assert padrao["colunas"] == ["n"]

    assert len((await amostra("?limite=100"))["amostra"]) == 100
    # Cap of 200 (clamp, not an error): ask for more, get what exists.
    assert len((await amostra("?limite=200"))["amostra"]) == 120
    # Out of range is 422 at the edge, not a silently different limit from the requested one.
    assert (
        await cliente.get(f"/execucoes/{eid}/nos/n1/preview?limite=201", headers=h)
    ).status_code == 422
    assert (
        await cliente.get(f"/execucoes/{eid}/nos/n1/preview?limite=0", headers=h)
    ).status_code == 422

    # A node that does not exist in this execution: 404 with the error envelope.
    r404 = await cliente.get(f"/execucoes/{eid}/nos/inexistente/preview", headers=h)
    assert r404.status_code == 404
    assert r404.json()["erro"]["codigo"] == "no_nao_encontrado"


async def test_dado_bruto_do_no_so_no_studio(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="dono-prev@example.com", perfil="especialista"
    )
    eid = await _pipeline_executado(cliente, dono)

    # Another specialist can see it (Studio: specialist/admin reads any execution).
    _, outro_esp = await usuario_logado(
        owner_engine, cliente, email="esp2-prev@example.com", perfil="especialista"
    )
    assert (
        await cliente.get(f"/execucoes/{eid}/nos/n1/preview", headers=outro_esp)
    ).status_code == 200

    # Operator: 403 even if the execution was triggered by THEM. Being able to follow it (status) is
    # not being able to see a node's raw data: through the Portal that would open columns the page
    # does not expose.
    _, op = await usuario_logado(
        owner_engine, cliente, email="op-prev@example.com", perfil="operador"
    )
    assert (await cliente.get(f"/execucoes/{eid}/nos/n1/preview", headers=op)).status_code == 403
    assert (await cliente.get(f"/execucoes/{eid}/nos/n1/preview")).status_code == 401


async def test_dado_bruto_de_pipeline_sistema_e_admin_only(owner_engine, app_engine, cliente):
    """A pipeline marked `sistema` exists to restrict DIRECT access to admins: its nodes' raw data
    must not escape through this route."""
    _, adm = await usuario_logado(
        owner_engine, cliente, email="adm-prev-sis@example.com", perfil="admin"
    )
    eid = await _pipeline_executado(cliente, adm)
    pid = (await cliente.get(f"/execucoes/{eid}", headers=adm)).json()["pipeline_id"]
    assert (
        await cliente.patch(f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200

    _, esp = await usuario_logado(
        owner_engine, cliente, email="esp-prev-sis@example.com", perfil="especialista"
    )
    assert (await cliente.get(f"/execucoes/{eid}/nos/n1/preview", headers=esp)).status_code == 403
    assert (await cliente.get(f"/execucoes/{eid}/nos/n1/preview", headers=adm)).status_code == 200
