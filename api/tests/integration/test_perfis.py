"""Assign a profile."""

from __future__ import annotations

import pytest

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def test_admin_altera_perfil(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adm@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="op@example.com", perfil="operador")
    r = await cliente.patch(f"/usuarios/{alvo}/perfil", json={"perfil": "especialista"}, headers=h)
    assert r.status_code == 204


async def test_nao_admin_negado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esp@example.com", perfil="especialista"
    )
    alvo = await semear_usuario(owner_engine, email="op2@example.com", perfil="operador")
    r = await cliente.patch(f"/usuarios/{alvo}/perfil", json={"perfil": "admin"}, headers=h)
    assert r.status_code == 403


async def test_perfil_invalido(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adm2@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="op3@example.com", perfil="operador")
    r = await cliente.patch(f"/usuarios/{alvo}/perfil", json={"perfil": "king"}, headers=h)
    assert r.status_code == 400
