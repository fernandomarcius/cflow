"""Protection of the last admin."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def test_nao_desativa_ultimo_admin(owner_engine, app_engine, cliente):
    admin_id, h = await usuario_logado(
        owner_engine, cliente, email="unico@example.com", perfil="admin"
    )
    # it is the only active admin -> cannot deactivate itself
    r = await cliente.patch(f"/usuarios/{admin_id}/ativo", json={"ativo": False}, headers=h)
    assert r.status_code == 409
    assert r.json()["erro"]["codigo"] == "ultimo_admin"


async def test_nao_rebaixa_ultimo_admin(owner_engine, app_engine, cliente):
    admin_id, h = await usuario_logado(
        owner_engine, cliente, email="unico2@example.com", perfil="admin"
    )
    r = await cliente.patch(f"/usuarios/{admin_id}/perfil", json={"perfil": "operador"}, headers=h)
    assert r.status_code == 409


async def test_com_dois_admins_pode_desativar_um(owner_engine, app_engine, cliente):
    from ._arnes_auth import semear_usuario

    admin_id, h = await usuario_logado(
        owner_engine, cliente, email="a1@example.com", perfil="admin"
    )
    outro = await semear_usuario(owner_engine, email="a2@example.com", perfil="admin")
    # with 2 active admins, deactivating one is allowed
    r = await cliente.patch(f"/usuarios/{outro}/ativo", json={"ativo": False}, headers=h)
    assert r.status_code == 204
