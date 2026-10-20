"""Groups: inheritance, effective permission (highest level), removal and cascade."""

from __future__ import annotations

import uuid

import pytest

from cflow_core.authz import permissoes as perm

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def _conceder(cliente, h, sujeito_tipo, sujeito_id, recurso, nivel):
    return await cliente.post(
        "/permissoes",
        json={
            "sujeito_tipo": sujeito_tipo,
            "sujeito_id": str(sujeito_id),
            "recurso_tipo": "pagina",
            "recurso_id": str(recurso),
            "nivel": nivel,
        },
        headers=h,
    )


async def test_heranca_max_remocao_e_cascade(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="ag@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="mg@example.com", perfil="operador")
    recurso = uuid.uuid4()

    gid = (await cliente.post("/grupos", json={"nome": "Operations"}, headers=h)).json()["id"]
    assert (
        await cliente.post(f"/grupos/{gid}/membros", json={"usuario_id": str(alvo)}, headers=h)
    ).status_code == 204
    await _conceder(cliente, h, "grupo", gid, recurso, "ver")

    async with app_engine.connect() as conn:
        assert await perm.nivel_efetivo(conn, alvo, "pagina", recurso) == "ver"

    # direct `editar` permission -> effective = the highest (editar)
    await _conceder(cliente, h, "usuario", alvo, recurso, "editar")
    async with app_engine.connect() as conn:
        assert await perm.nivel_efetivo(conn, alvo, "pagina", recurso) == "editar"

    # revoke the direct permission: the group's `ver` remains
    assert (
        await cliente.request(
            "DELETE",
            "/permissoes",
            json={
                "sujeito_tipo": "usuario",
                "sujeito_id": str(alvo),
                "recurso_tipo": "pagina",
                "recurso_id": str(recurso),
                "nivel": "editar",
            },
            headers=h,
        )
    ).status_code == 204
    async with app_engine.connect() as conn:
        assert (
            await perm.nivel_efetivo(conn, alvo, "pagina", recurso) == "ver"
        )  # inherited from the group

    assert (await cliente.delete(f"/grupos/{gid}/membros/{alvo}", headers=h)).status_code == 204
    async with app_engine.connect() as conn:
        assert (
            await perm.nivel_efetivo(conn, alvo, "pagina", recurso) is None
        )  # lost the inheritance


async def test_listar_grupos_com_membros(owner_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="ag3@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="mg3@example.com", perfil="operador")
    r = await cliente.post(
        "/grupos", json={"nome": "Auditoria", "descricao": "read access"}, headers=h
    )
    gid = r.json()["id"]
    await cliente.post(f"/grupos/{gid}/membros", json={"usuario_id": str(alvo)}, headers=h)

    r = await cliente.get("/grupos", headers=h)
    assert r.status_code == 200
    grupos = {g["nome"]: g for g in r.json()}
    assert grupos["Auditoria"]["descricao"] == "read access"
    assert [m["email"] for m in grupos["Auditoria"]["membros"]] == ["mg3@example.com"]


async def test_listar_grupos_nega_nao_admin(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esp-g@example.com", perfil="especialista"
    )
    assert (await cliente.get("/grupos", headers=h)).status_code == 403


async def test_excluir_grupo_faz_cascade(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="ag2@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="mg2@example.com", perfil="operador")
    recurso = uuid.uuid4()
    gid = (await cliente.post("/grupos", json={"nome": "Board"}, headers=h)).json()["id"]
    await cliente.post(f"/grupos/{gid}/membros", json={"usuario_id": str(alvo)}, headers=h)
    await _conceder(cliente, h, "grupo", gid, recurso, "ver")

    assert (await cliente.delete(f"/grupos/{gid}", headers=h)).status_code == 204
    async with app_engine.connect() as conn:
        # the membership and the group's permission are gone -> no access
        assert await perm.nivel_efetivo(conn, alvo, "pagina", recurso) is None
