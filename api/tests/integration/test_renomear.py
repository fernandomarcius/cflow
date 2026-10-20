"""Rename resources (pipeline/cube/view/page): PATCH /<recurso>/{id} {nome}.

Covers: happy path + trail record (acao=editar, before/after diff), denied authorization
(another specialist without a grant -> 403), empty name -> 400. The creator administers; admin bypasses.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _aud_renomear(owner_engine, recurso_tipo: str, recurso_id: str) -> dict | None:
    async with owner_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, antes, depois FROM auditoria "
                        "WHERE recurso_tipo = :t AND recurso_id = :r AND acao = 'editar' "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"t": recurso_tipo, "r": recurso_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def test_renomear_pipeline_feliz_e_audita(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rnp@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "old"}, headers=h)).json()["id"]

    r = await cliente.patch(f"/pipelines/{pid}", json={"nome": "  new name  "}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": pid, "nome": "new name"}  # trim applied

    # persisted name
    det = await cliente.get(f"/pipelines/{pid}", headers=h)
    assert det.json()["nome"] == "new name"

    # trail: acao=editar, before/after diff of the name
    aud = await _aud_renomear(owner_engine, "pipeline", pid)
    assert aud is not None
    assert aud["acao"] == "editar"
    assert aud["antes"] == {"nome": "old"} and aud["depois"] == {"nome": "new name"}


async def test_renomear_pipeline_negado_para_nao_dono(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="rndono@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "owners-own"}, headers=dono)).json()["id"]

    _, outro = await usuario_logado(
        owner_engine, cliente, email="rnoutro@example.com", perfil="especialista"
    )
    neg = await cliente.patch(f"/pipelines/{pid}", json={"nome": "hack"}, headers=outro)
    assert neg.status_code == 403 and neg.json()["erro"]["codigo"] == "acesso_negado"

    # admin bypassa
    _, adm = await usuario_logado(owner_engine, cliente, email="rnadm@example.com", perfil="admin")
    ok = await cliente.patch(f"/pipelines/{pid}", json={"nome": "renamed-by-admin"}, headers=adm)
    assert ok.status_code == 200 and ok.json()["nome"] == "renamed-by-admin"


async def test_renomear_nome_vazio_400(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rnvazio@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "x"}, headers=h)).json()["id"]
    r = await cliente.patch(f"/pipelines/{pid}", json={"nome": "   "}, headers=h)
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "nome_invalido"


async def test_renomear_cubo_view_pagina(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rnmodel@example.com", perfil="especialista"
    )
    for base, recurso_tipo in (("/cubos", "cubo"), ("/views", "view"), ("/paginas", "pagina")):
        rid = (await cliente.post(base, json={"nome": "former"}, headers=h)).json()["id"]
        r = await cliente.patch(f"{base}/{rid}", json={"nome": "modern"}, headers=h)
        assert r.status_code == 200, (base, r.text)
        assert r.json()["nome"] == "modern"
        aud = await _aud_renomear(owner_engine, recurso_tipo, rid)
        assert aud is not None and aud["depois"] == {"nome": "modern"}


async def test_renomear_view_negado_para_nao_dono(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="rnvdono@example.com", perfil="especialista"
    )
    vid = (await cliente.post("/views", json={"nome": "my-view"}, headers=dono)).json()["id"]
    _, outro = await usuario_logado(
        owner_engine, cliente, email="rnvoutro@example.com", perfil="especialista"
    )
    neg = await cliente.patch(f"/views/{vid}", json={"nome": "theft"}, headers=outro)
    assert neg.status_code == 403
