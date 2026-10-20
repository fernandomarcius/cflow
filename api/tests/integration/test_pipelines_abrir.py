"""Open/list a pipeline: save and reopen the definition (including node positions)."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


async def test_salvar_e_reabrir(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="ab@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Orders migration"}, headers=h)).json()[
        "id"
    ]

    definicao = {
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a", "posicao": {"x": 10, "y": 20}}],
        "arestas": [],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200 and r.json()["numero"] == 1

    det = await cliente.get(f"/pipelines/{pid}", headers=h)
    assert det.status_code == 200
    corpo = det.json()
    assert corpo["nome"] == "Orders migration"
    assert corpo["numero"] == 1
    no = corpo["definicao"]["nos"][0]
    assert no["id"] == "n1" and no["sql"] == "SELECT 1 AS a"
    assert no["posicao"] == {"x": 10, "y": 20}  # position preserved


async def test_listar_inclui_pipeline(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="ab2@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Portfolio"}, headers=h)).json()["id"]
    lst = await cliente.get("/pipelines", headers=h)
    assert lst.status_code == 200
    assert pid in [p["id"] for p in lst.json()]


async def test_obter_inexistente_404(owner_engine, app_engine, cliente):
    import uuid

    _, h = await usuario_logado(
        owner_engine, cliente, email="ab3@example.com", perfil="especialista"
    )
    r = await cliente.get(f"/pipelines/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404


async def test_excluir_pipeline_remove_e_audita(owner_engine, app_engine, cliente):
    from sqlalchemy import text

    _, h = await usuario_logado(
        owner_engine, cliente, email="del@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "To Delete"}, headers=h)).json()["id"]
    # generate dependencies (published version + execution + nodes) to prove the cascade
    definicao = {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    eid = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, eid, h)  # workflow finished before deleting (clean cascade)
    await cliente.post(
        f"/pipelines/{pid}/agendamentos", json={"intervalo_segundos": 3600}, headers=h
    )

    r = await cliente.delete(f"/pipelines/{pid}", headers=h)
    assert r.status_code == 204

    # gone: GET 404, out of the listing, dependencies deleted
    assert (await cliente.get(f"/pipelines/{pid}", headers=h)).status_code == 404
    assert pid not in [p["id"] for p in (await cliente.get("/pipelines", headers=h)).json()]
    async with owner_engine.connect() as conn:
        for tab, col in [
            ("pipeline_versoes", "pipeline_id"),
            ("execucoes", "pipeline_id"),
            ("agendamentos", "pipeline_id"),
            ("permissoes", "recurso_id"),
        ]:
            n = (
                await conn.execute(text(f"SELECT count(*) FROM {tab} WHERE {col} = :p"), {"p": pid})
            ).scalar_one()
            assert n == 0, f"{tab} ainda tem linhas"
        n_aud = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao = 'excluir' AND recurso_tipo = 'pipeline' AND recurso_id = :p"
                ),
                {"p": pid},
            )
        ).scalar_one()
        assert n_aud == 1  # the trail recorded the deletion


async def test_excluir_pipeline_authz_negada(owner_engine, app_engine, cliente):
    # creator (specialist) creates; another specialist WITHOUT permission on the resource -> 403
    _, dono = await usuario_logado(
        owner_engine, cliente, email="dono@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Protected"}, headers=dono)).json()["id"]
    _, outro = await usuario_logado(
        owner_engine, cliente, email="intruso@example.com", perfil="especialista"
    )
    r = await cliente.delete(f"/pipelines/{pid}", headers=outro)
    assert r.status_code == 403
    # segue existindo
    assert (await cliente.get(f"/pipelines/{pid}", headers=dono)).status_code == 200
