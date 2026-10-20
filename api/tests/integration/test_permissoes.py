"""Per-resource permissions."""

from __future__ import annotations

import uuid

import pytest

from cflow_core.authz import permissoes as perm

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def test_nivel_cumulativo_e_revogacao(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="ap@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="pu@example.com", perfil="operador")
    recurso = uuid.uuid4()
    body = {
        "sujeito_tipo": "usuario",
        "sujeito_id": str(alvo),
        "recurso_tipo": "pagina",
        "recurso_id": str(recurso),
        "nivel": "editar",
    }
    # grant `editar`
    r = await cliente.post("/permissoes", json=body, headers=h)
    assert r.status_code == 201 and r.json()["criada"] is True

    async with app_engine.connect() as conn:
        assert await perm.tem_permissao(conn, alvo, "pagina", recurso, "ver") is True
        assert await perm.tem_permissao(conn, alvo, "pagina", recurso, "executar") is True
        assert await perm.tem_permissao(conn, alvo, "pagina", recurso, "editar") is True
        assert await perm.tem_permissao(conn, alvo, "pagina", recurso, "administrar") is False

    # idempotency: granting again does not duplicate
    assert (await cliente.post("/permissoes", json=body, headers=h)).json()["criada"] is False

    # revoke
    assert (await cliente.request("DELETE", "/permissoes", json=body, headers=h)).status_code == 204
    async with app_engine.connect() as conn:
        assert await perm.tem_permissao(conn, alvo, "pagina", recurso, "ver") is False


async def test_sem_permissao_nega(owner_engine, app_engine, cliente):
    alvo = await semear_usuario(owner_engine, email="np@example.com", perfil="operador")
    async with app_engine.connect() as conn:
        assert await perm.tem_permissao(conn, alvo, "pagina", uuid.uuid4(), "ver") is False
